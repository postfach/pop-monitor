#include "config.h"
#include "util.h"

#include <cjson/cJSON.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* Copy a string field. "${NAME}" is replaced by the environment variable,
 * so secrets can stay out of the config file. */
static void get_str(const cJSON *o, const char *k, char *dst, size_t n)
{
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(o, k);
    if (!cJSON_IsString(v) || !v->valuestring) return;
    const char *s = v->valuestring;
    size_t w = 0;
    while (*s && w + 1 < n) {
        const char *end;
        if (s[0] == '$' && s[1] == '{' && (end = strchr(s + 2, '}'))) {
            char name[128];
            snprintf(name, sizeof name, "%.*s", (int)(end - s - 2), s + 2);
            const char *e = getenv(name);
            if (!e) LOGW("config", "environment variable %s is not set", name);
            for (; e && *e && w + 1 < n; e++) dst[w++] = *e;
            s = end + 1;
        } else {
            dst[w++] = *s++;
        }
    }
    dst[w] = 0;
}

static void get_int(const cJSON *o, const char *k, int *dst)
{
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(o, k);
    if (cJSON_IsNumber(v)) *dst = v->valueint;
    else if (cJSON_IsBool(v)) *dst = cJSON_IsTrue(v);
}

static void get_long(const cJSON *o, const char *k, long *dst)
{
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(o, k);
    if (cJSON_IsNumber(v)) *dst = (long)v->valuedouble;
}

static void get_float(const cJSON *o, const char *k, float *dst)
{
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(o, k);
    if (cJSON_IsNumber(v)) *dst = (float)v->valuedouble;
}

static void get_profile(const cJSON *o, pc_profile *p)
{
    if (!cJSON_IsObject(o)) return;
    get_int(o, "slot_count", &p->slot_count);
    get_int(o, "slot_ms", &p->slot_ms);
}

/* Percent-encode credentials for use inside an RTSP URL. */
static void url_encode(const char *in, char *out, size_t n)
{
    static const char *hex = "0123456789ABCDEF";
    size_t o = 0;
    for (; *in && o + 4 < n; in++) {
        unsigned char ch = (unsigned char)*in;
        if ((ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z') || (ch >= '0' && ch <= '9') ||
            ch == '-' || ch == '_' || ch == '.' || ch == '~') {
            out[o++] = (char)ch;
        } else {
            out[o++] = '%';
            out[o++] = hex[ch >> 4];
            out[o++] = hex[ch & 15];
        }
    }
    out[o] = 0;
}

static void camera_defaults(camera_cfg *c)
{
    memset(c, 0, sizeof *c);
    pc_point full[4] = {{0, 0}, {1, 0}, {1, 1}, {0, 1}};
    memcpy(c->roi, full, sizeof full);
    c->inset_pct = 3.f;
    c->analysis_fps = 5;
    snprintf(c->operating_hours, sizeof c->operating_hours, "00:00-24:00");
    c->ptz_check = 1;
    snprintf(c->rtsp_transport, sizeof c->rtsp_transport, "tcp");
    snprintf(c->stream_params, sizeof c->stream_params, "resolution=1280x720&fps=10&videocodec=h264");
    c->ptz_tolerance = 1.0f;
    pc_detector_cfg_default(&c->det);
    pc_faults_cfg_default(&c->faults);
    c->snap.delay_ms = 1000;
    c->snap.burst = 3;
    c->snap.interval_ms = 300;
    c->snap.compression = 15;
    c->snap.roi_width = 1024;
    c->snap.roi_quality = 92;
}

/* Apply any fields present in `o` (used for "defaults" then each camera). */
static void apply_camera(camera_cfg *c, const cJSON *o)
{
    if (!cJSON_IsObject(o)) return;
    get_str(o, "site_id", c->site_id, sizeof c->site_id);
    get_str(o, "name", c->name, sizeof c->name);
    get_str(o, "model", c->model, sizeof c->model);
    get_str(o, "host", c->host, sizeof c->host);
    get_str(o, "username", c->username, sizeof c->username);
    get_str(o, "password", c->password, sizeof c->password);
    get_str(o, "rtsp_url", c->rtsp_url, sizeof c->rtsp_url);
    get_str(o, "vapix_url", c->vapix_url, sizeof c->vapix_url);
    get_int(o, "http_port", &c->http_port);
    get_int(o, "rtsp_port", &c->rtsp_port);
    get_int(o, "use_https", &c->use_https);
    get_int(o, "camera_tls_insecure", &c->camera_tls_insecure);
    get_str(o, "rtsp_transport", c->rtsp_transport, sizeof c->rtsp_transport);
    get_str(o, "stream_params", c->stream_params, sizeof c->stream_params);
    get_str(o, "operating_hours", c->operating_hours, sizeof c->operating_hours);
    get_float(o, "inset_pct", &c->inset_pct);
    get_int(o, "analysis_fps", &c->analysis_fps);
    get_int(o, "ptz_check", &c->ptz_check);
    get_float(o, "ptz_tolerance", &c->ptz_tolerance);
    get_int(o, "realtime", &c->realtime);
    get_int(o, "loop", &c->loop);

    const cJSON *roi = cJSON_GetObjectItemCaseSensitive(o, "roi");
    if (cJSON_IsArray(roi) && cJSON_GetArraySize(roi) == 4) {
        for (int i = 0; i < 4; i++) {
            const cJSON *pt = cJSON_GetArrayItem(roi, i);
            if (cJSON_IsArray(pt) && cJSON_GetArraySize(pt) == 2) {
                c->roi[i].x = (float)cJSON_GetArrayItem(pt, 0)->valuedouble;
                c->roi[i].y = (float)cJSON_GetArrayItem(pt, 1)->valuedouble;
            }
        }
    }

    get_profile(cJSON_GetObjectItemCaseSensitive(o, "profile"), &c->profile);
    const cJSON *take = cJSON_GetObjectItemCaseSensitive(o, "takeover");
    if (cJSON_IsTrue(take)) c->profile.slot_count = 1;

    const cJSON *dp = cJSON_GetObjectItemCaseSensitive(o, "dayparts");
    if (cJSON_IsArray(dp)) {
        c->n_dayparts = 0;
        const cJSON *it;
        cJSON_ArrayForEach(it, dp) {
            if (c->n_dayparts >= MAX_DAYPARTS) break;
            daypart *d = &c->dayparts[c->n_dayparts++];
            memset(d, 0, sizeof *d);
            get_str(it, "from", d->from, sizeof d->from);
            get_str(it, "to", d->to, sizeof d->to);
            get_profile(it, &d->profile);
        }
    }

    const cJSON *det = cJSON_GetObjectItemCaseSensitive(o, "detector");
    if (cJSON_IsObject(det)) {
        get_float(det, "cell_delta", &c->det.cell_delta);
        get_float(det, "cut_fraction", &c->det.cut_fraction);
        get_float(det, "settle_fraction", &c->det.settle_fraction);
        get_int(det, "settle_ms", &c->det.settle_ms);
        get_float(det, "drift_distance", &c->det.drift_distance);
        get_int(det, "new_hash_dist", &c->det.new_hash_dist);
        get_float(det, "new_grid_dist", &c->det.new_grid_dist);
        get_int(det, "min_slot_ms", &c->det.min_slot_ms);
        get_int(det, "motion_play_ms", &c->det.motion_play_ms);
        get_float(det, "black_mean", &c->det.black_mean);
        get_float(det, "blank_std", &c->det.blank_std);
    }
    const cJSON *f = cJSON_GetObjectItemCaseSensitive(o, "faults");
    if (cJSON_IsObject(f)) {
        get_int(f, "black_ms", &c->faults.black_ms);
        get_int(f, "blank_ms", &c->faults.blank_ms);
        get_int(f, "partial_ms", &c->faults.partial_ms);
        get_int(f, "frozen_default_ms", &c->faults.frozen_default_ms);
        get_float(f, "blur_ratio", &c->faults.blur_ratio);
        get_int(f, "blur_window", &c->faults.blur_window);
    }
    const cJSON *s = cJSON_GetObjectItemCaseSensitive(o, "snapshot");
    if (cJSON_IsObject(s)) {
        get_int(s, "delay_ms", &c->snap.delay_ms);
        get_int(s, "burst", &c->snap.burst);
        get_int(s, "interval_ms", &c->snap.interval_ms);
        get_int(s, "compression", &c->snap.compression);
        get_str(s, "resolution", c->snap.resolution, sizeof c->snap.resolution);
        get_int(s, "roi_width", &c->snap.roi_width);
        get_int(s, "roi_quality", &c->snap.roi_quality);
        char pol[32] = "";
        get_str(s, "image_policy", pol, sizeof pol);
        if (*pol) c->snap.first_per_hour = strcmp(pol, "first_per_hour") == 0;
    }
}

static int validate_camera(camera_cfg *c, int idx, char *err, size_t n)
{
    if (!*c->site_id) { snprintf(err, n, "camera %d: site_id is required", idx); return -1; }
    if (!*c->name) snprintf(c->name, sizeof c->name, "%s", c->site_id);
    if (!*c->rtsp_url && !*c->host) {
        snprintf(err, n, "camera %s: host or rtsp_url is required", c->name);
        return -1;
    }
    if (strcmp(c->rtsp_transport, "tcp") && strcmp(c->rtsp_transport, "http")) {
        snprintf(err, n, "camera %s: rtsp_transport must be \"tcp\" or \"http\"", c->name);
        return -1;
    }
    int web_port = c->http_port ? c->http_port : (c->use_https ? 443 : 80);
    int default_web = c->use_https ? 443 : 80;
    char webport[16] = "", rport[16] = "";
    if (web_port != default_web) snprintf(webport, sizeof webport, ":%d", web_port);
    if (!strcmp(c->rtsp_transport, "http")) {
        /* RTSP tunnelled over HTTP: uses the camera's web port */
        if (web_port != 80) snprintf(rport, sizeof rport, ":%d", web_port);
    } else if (c->rtsp_port && c->rtsp_port != 554) {
        snprintf(rport, sizeof rport, ":%d", c->rtsp_port);
    }
    if (!*c->rtsp_url) {
        char cred[420] = "";
        if (*c->username) {
            char u[200], p[200];
            url_encode(c->username, u, sizeof u);
            url_encode(c->password, p, sizeof p);
            snprintf(cred, sizeof cred, "%s:%s@", u, p);
        }
        snprintf(c->rtsp_url, sizeof c->rtsp_url, "rtsp://%s%s%s/axis-media/media.amp?%s",
                 cred, c->host, rport, c->stream_params);
    }
    if (!*c->vapix_url && *c->host)
        snprintf(c->vapix_url, sizeof c->vapix_url, "%s://%s%s", c->use_https ? "https" : "http",
                 c->host, webport);
    if (c->analysis_fps < 1) c->analysis_fps = 1;
    if (c->analysis_fps > 15) c->analysis_fps = 15;
    if (c->snap.burst < 1) c->snap.burst = 1;
    if (c->snap.burst > 8) c->snap.burst = 8;
    for (int i = 0; i < 4; i++)
        if (c->roi[i].x < 0 || c->roi[i].x > 1 || c->roi[i].y < 0 || c->roi[i].y > 1) {
            snprintf(err, n, "camera %s: roi points must be normalised 0..1", c->name);
            return -1;
        }
    return 0;
}

int config_load(const char *path, gateway_cfg *g, char *err, size_t n)
{
    memset(g, 0, sizeof *g);
    size_t len;
    char *txt = util_read_file(path, &len);
    if (!txt) { snprintf(err, n, "cannot read %s", path); return -1; }
    cJSON *root = cJSON_Parse(txt);
    free(txt);
    if (!root) { snprintf(err, n, "%s: invalid JSON near '%.20s'", path, cJSON_GetErrorPtr()); return -1; }

    snprintf(g->queue_dir, sizeof g->queue_dir, "/var/lib/pop-gateway/queue");
    g->queue_max_mb = 20000;
    g->heartbeat_s = 300;
    g->alert_cooldown_s = 1800;
    g->log_level = LOG_INFO;
    get_str(root, "gateway_id", g->gateway_id, sizeof g->gateway_id);
    get_str(root, "server_url", g->server_url, sizeof g->server_url);
    get_str(root, "api_key", g->api_key, sizeof g->api_key);
    get_str(root, "hmac_key", g->hmac_key, sizeof g->hmac_key);
    get_str(root, "queue_dir", g->queue_dir, sizeof g->queue_dir);
    get_long(root, "queue_max_mb", &g->queue_max_mb);
    get_int(root, "heartbeat_s", &g->heartbeat_s);
    get_int(root, "tls_insecure", &g->tls_insecure);
    char lvl[16] = "";
    get_str(root, "log_level", lvl, sizeof lvl);
    if (!strcmp(lvl, "debug")) g->log_level = LOG_DEBUG;
    else if (!strcmp(lvl, "warn")) g->log_level = LOG_WARN;
    const cJSON *al = cJSON_GetObjectItemCaseSensitive(root, "alerts");
    if (cJSON_IsObject(al)) {
        get_str(al, "teams_webhook", g->teams_webhook, sizeof g->teams_webhook);
        get_str(al, "slack_webhook", g->slack_webhook, sizeof g->slack_webhook);
        get_int(al, "cooldown_s", &g->alert_cooldown_s);
    }
    if (!*g->gateway_id) snprintf(g->gateway_id, sizeof g->gateway_id, "pop-gateway");
    if (!*g->server_url) { snprintf(err, n, "server_url is required"); cJSON_Delete(root); return -1; }
    size_t L = strlen(g->server_url);
    if (L && g->server_url[L - 1] == '/') g->server_url[L - 1] = 0;

    const cJSON *defs = cJSON_GetObjectItemCaseSensitive(root, "defaults");
    const cJSON *cams = cJSON_GetObjectItemCaseSensitive(root, "cameras");
    int nc = cJSON_IsArray(cams) ? cJSON_GetArraySize(cams) : 0;
    if (nc == 0) { snprintf(err, n, "no cameras configured"); cJSON_Delete(root); return -1; }
    g->cameras = calloc((size_t)nc, sizeof(camera_cfg));
    for (int i = 0; i < nc; i++) {
        camera_cfg *c = &g->cameras[i];
        camera_defaults(c);
        apply_camera(c, defs);
        apply_camera(c, cJSON_GetArrayItem(cams, i));
        if (validate_camera(c, i, err, n) != 0) { cJSON_Delete(root); config_free(g); return -1; }
    }
    g->n_cameras = nc;
    cJSON_Delete(root);
    return 0;
}

void config_free(gateway_cfg *g)
{
    free(g->cameras);
    g->cameras = NULL;
    g->n_cameras = 0;
}

pc_profile config_active_profile(const camera_cfg *c, int64_t ms)
{
    for (int i = 0; i < c->n_dayparts; i++) {
        char range[24];
        snprintf(range, sizeof range, "%s-%s", c->dayparts[i].from, c->dayparts[i].to);
        if (util_in_time_range(range, ms)) return c->dayparts[i].profile;
    }
    return c->profile;
}
