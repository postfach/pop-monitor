/* pop-gateway: Proof-of-Play monitor for Axis cameras that can't run an ACAP.
 *
 *   pop-gateway -c /etc/pop-gateway/config.json
 *   pop-gateway -c config.json --check      validate config and exit
 */
#include "alerts.h"
#include "app.h"
#include "camera.h"
#include "config.h"
#include "queue.h"
#include "uploader.h"
#include "util.h"

#include <curl/curl.h>
#include <libavformat/avformat.h>
#include <libavutil/log.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static app_ctx app;

static void on_signal(int sig)
{
    (void)sig;
    app.running = 0;
}

static void usage(void)
{
    fprintf(stderr, "usage: pop-gateway -c config.json [--check] [-v]\n");
}

int main(int argc, char **argv)
{
    const char *cfg_path = NULL;
    int check = 0, verbose = 0;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "-c") && i + 1 < argc) cfg_path = argv[++i];
        else if (!strcmp(argv[i], "--check")) check = 1;
        else if (!strcmp(argv[i], "-v")) verbose = 1;
        else if (!strcmp(argv[i], "--version")) { printf("pop-gateway %s\n", PC_VERSION); return 0; }
        else { usage(); return 2; }
    }
    if (!cfg_path) { usage(); return 2; }

    gateway_cfg cfg;
    char err[512];
    if (config_load(cfg_path, &cfg, err, sizeof err) != 0) {
        fprintf(stderr, "config error: %s\n", err);
        return 1;
    }
    util_log_level = verbose ? LOG_DEBUG : cfg.log_level;
    if (check) {
        printf("config OK: gateway %s, %d camera(s), server %s\n", cfg.gateway_id, cfg.n_cameras, cfg.server_url);
        for (int i = 0; i < cfg.n_cameras; i++) {
            camera_cfg *c = &cfg.cameras[i];
            printf("  %-24s site=%-20s profile=%dx%dms dayparts=%d hours=%s\n", c->name, c->site_id,
                   c->profile.slot_count, c->profile.slot_ms, c->n_dayparts, c->operating_hours);
        }
        config_free(&cfg);
        return 0;
    }

    if (queue_init(cfg.queue_dir) != 0) {
        fprintf(stderr, "cannot create queue dir %s\n", cfg.queue_dir);
        return 1;
    }
    curl_global_init(CURL_GLOBAL_DEFAULT);
    avformat_network_init();
    av_log_set_level(verbose ? AV_LOG_WARNING : AV_LOG_ERROR);

    app.cfg = &cfg;
    app.running = 1;
    app.status = calloc((size_t)cfg.n_cameras, sizeof(cam_status));
    for (int i = 0; i < cfg.n_cameras; i++) pthread_mutex_init(&app.status[i].mu, NULL);

    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = on_signal;
    sigaction(SIGINT, &sa, NULL);
    sigaction(SIGTERM, &sa, NULL);
    signal(SIGPIPE, SIG_IGN);

    LOGI("main", "pop-gateway %s starting: %d camera(s), server %s, queue %s",
         PC_VERSION, cfg.n_cameras, cfg.server_url, cfg.queue_dir);
    if (!util_clock_synced()) LOGW("main", "system clock is not NTP-synchronised; PoP timestamps may be off");

    alerts_start(&app);
    uploader_start(&app);
    for (int i = 0; i < cfg.n_cameras; i++)
        if (camera_start(&app, i) != 0) LOGE("main", "failed to start camera %s", cfg.cameras[i].name);

    while (app.running) util_sleep_ms(250);

    LOGI("main", "shutting down");
    camera_join_all();
    uploader_join();
    alerts_stop();
    curl_global_cleanup();
    free(app.status);
    config_free(&cfg);
    return 0;
}
