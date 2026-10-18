#include "alerts.h"
#include "queue.h"
#include "util.h"

#include <cjson/cJSON.h>
#include <curl/curl.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define DIRECT_Q 64
#define COOLDOWN_SLOTS 1024

typedef struct { char *url; char *body; } direct_msg;

static app_ctx *g_app;
static pthread_t thr;
static pthread_mutex_t mu = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t cv = PTHREAD_COND_INITIALIZER;
static direct_msg q[DIRECT_Q];
static int qh, qn, stopping;

/* cooldown memory: (camera, fault) -> last direct notification */
static struct { char key[160]; int64_t last; int notified_raise; } cool[COOLDOWN_SLOTS];

static size_t sink(void *p, size_t s, size_t n, void *u) { (void)p; (void)u; return s * n; }

long http_post_json(const char *url, const char *json, const char *bearer, int insecure, int timeout_s)
{
    CURL *c = curl_easy_init();
    if (!c) return -1;
    struct curl_slist *h = curl_slist_append(NULL, "Content-Type: application/json");
    char auth[400];
    if (bearer && *bearer) {
        snprintf(auth, sizeof auth, "Authorization: Bearer %s", bearer);
        h = curl_slist_append(h, auth);
    }
    curl_easy_setopt(c, CURLOPT_URL, url);
    curl_easy_setopt(c, CURLOPT_HTTPHEADER, h);
    curl_easy_setopt(c, CURLOPT_POSTFIELDS, json);
    curl_easy_setopt(c, CURLOPT_TIMEOUT, (long)timeout_s);
    curl_easy_setopt(c, CURLOPT_NOSIGNAL, 1L);
    curl_easy_setopt(c, CURLOPT_WRITEFUNCTION, sink);
    if (insecure) {
        curl_easy_setopt(c, CURLOPT_SSL_VERIFYPEER, 0L);
        curl_easy_setopt(c, CURLOPT_SSL_VERIFYHOST, 0L);
    }
    long status = -1;
    if (curl_easy_perform(c) == CURLE_OK) curl_easy_getinfo(c, CURLINFO_RESPONSE_CODE, &status);
    curl_slist_free_all(h);
    curl_easy_cleanup(c);
    return status;
}

static const char *severity_of(const char *fault)
{
    static const char *crit[] = {"black", "blank", "frozen", "stream_lost", NULL};
    for (int i = 0; crit[i]; i++)
        if (!strcmp(fault, crit[i])) return "critical";
    return "warning";
}

static void direct_push(const char *url, char *body)
{
    pthread_mutex_lock(&mu);
    if (qn == DIRECT_Q) {               /* full: drop oldest */
        direct_msg *old = &q[qh];
        free(old->url);
        free(old->body);
        qh = (qh + 1) % DIRECT_Q;
        qn--;
    }
    direct_msg *m = &q[(qh + qn) % DIRECT_Q];
    m->url = strdup(url);
    m->body = body;
    qn++;
    pthread_cond_signal(&cv);
    pthread_mutex_unlock(&mu);
}

static void *direct_thread(void *arg)
{
    (void)arg;
    for (;;) {
        pthread_mutex_lock(&mu);
        while (qn == 0 && !stopping) pthread_cond_wait(&cv, &mu);
        if (qn == 0 && stopping) { pthread_mutex_unlock(&mu); break; }
        direct_msg m = q[qh];
        qh = (qh + 1) % DIRECT_Q;
        qn--;
        pthread_mutex_unlock(&mu);
        for (int attempt = 0; attempt < 3; attempt++) {
            long st = http_post_json(m.url, m.body, NULL, 0, 15);
            if (st >= 200 && st < 300) break;
            LOGW("alerts", "webhook post failed (HTTP %ld), attempt %d", st, attempt + 1);
            util_sleep_ms(2000 * (attempt + 1));
        }
        free(m.url);
        free(m.body);
    }
    return NULL;
}

int alerts_start(app_ctx *app)
{
    g_app = app;
    return pthread_create(&thr, NULL, direct_thread, NULL);
}

void alerts_stop(void)
{
    pthread_mutex_lock(&mu);
    stopping = 1;
    pthread_cond_signal(&cv);
    pthread_mutex_unlock(&mu);
    pthread_join(thr, NULL);
}

/* Returns 1 if a direct (Teams/Slack) notification should go out. */
static int cooldown_ok(const char *key, const char *state, int cooldown_s, int64_t now)
{
    int slot = -1, empty = -1;
    for (int i = 0; i < COOLDOWN_SLOTS; i++) {
        if (!strcmp(cool[i].key, key)) { slot = i; break; }
        if (empty < 0 && !cool[i].key[0]) empty = i;
    }
    if (slot < 0) {
        if (empty < 0) empty = (int)(now % COOLDOWN_SLOTS);
        slot = empty;
        snprintf(cool[slot].key, sizeof cool[slot].key, "%s", key);
        cool[slot].last = 0;
        cool[slot].notified_raise = 0;
    }
    if (!strcmp(state, "cleared")) {          /* only if we told them it broke */
        int ok = cool[slot].notified_raise;
        cool[slot].notified_raise = 0;
        return ok;
    }
    if (cool[slot].last && now - cool[slot].last < (int64_t)cooldown_s * 1000) return 0;
    cool[slot].last = now;
    if (!strcmp(state, "raised")) cool[slot].notified_raise = 1;
    return 1;
}

static char *teams_payload(const camera_cfg *cam, const char *fault, const char *state,
                           const char *sev, const char *when, const char *detail)
{
    char title[200];
    snprintf(title, sizeof title, "%s%s %s at %s",
             !strcmp(state, "cleared") ? "Recovered: " : (!strcmp(sev, "critical") ? "Critical: " : ""),
             fault, state, cam->name);
    cJSON *root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "type", "message");
    cJSON *atts = cJSON_AddArrayToObject(root, "attachments");
    cJSON *att = cJSON_CreateObject();
    cJSON_AddItemToArray(atts, att);
    cJSON_AddStringToObject(att, "contentType", "application/vnd.microsoft.card.adaptive");
    cJSON *card = cJSON_AddObjectToObject(att, "content");
    cJSON_AddStringToObject(card, "$schema", "http://adaptivecards.io/schemas/adaptive-card.json");
    cJSON_AddStringToObject(card, "type", "AdaptiveCard");
    cJSON_AddStringToObject(card, "version", "1.4");
    cJSON *body = cJSON_AddArrayToObject(card, "body");
    cJSON *tb = cJSON_CreateObject();
    cJSON_AddStringToObject(tb, "type", "TextBlock");
    cJSON_AddStringToObject(tb, "size", "Medium");
    cJSON_AddStringToObject(tb, "weight", "Bolder");
    cJSON_AddStringToObject(tb, "text", title);
    cJSON_AddBoolToObject(tb, "wrap", 1);
    cJSON_AddItemToArray(body, tb);
    cJSON *fs = cJSON_CreateObject();
    cJSON_AddStringToObject(fs, "type", "FactSet");
    cJSON *facts = cJSON_AddArrayToObject(fs, "facts");
    const char *kv[][2] = {{"Site", cam->name}, {"Site ID", cam->site_id}, {"Fault", fault},
                           {"State", state}, {"Severity", sev}, {"Time", when},
                           {"Detail", detail ? detail : ""}};
    for (size_t i = 0; i < sizeof kv / sizeof kv[0]; i++) {
        cJSON *f = cJSON_CreateObject();
        cJSON_AddStringToObject(f, "title", kv[i][0]);
        cJSON_AddStringToObject(f, "value", kv[i][1]);
        cJSON_AddItemToArray(facts, f);
    }
    cJSON_AddItemToArray(body, fs);
    char *s = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    return s;
}

static char *slack_payload(const camera_cfg *cam, const char *fault, const char *state,
                           const char *sev, const char *when, const char *detail)
{
    char text[600];
    snprintf(text, sizeof text, "%s *%s %s* at *%s* (site ID %s)\n%s\n_%s_",
             !strcmp(state, "cleared") ? ":white_check_mark:" : (!strcmp(sev, "critical") ? ":red_circle:" : ":warning:"),
             fault, state, cam->name, cam->site_id, detail ? detail : "", when);
    cJSON *root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "text", text);
    cJSON *blocks = cJSON_AddArrayToObject(root, "blocks");
    cJSON *b = cJSON_CreateObject();
    cJSON_AddStringToObject(b, "type", "section");
    cJSON *t = cJSON_AddObjectToObject(b, "text");
    cJSON_AddStringToObject(t, "type", "mrkdwn");
    cJSON_AddStringToObject(t, "text", text);
    cJSON_AddItemToArray(blocks, b);
    char *s = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    return s;
}

void alerts_send(app_ctx *app, const camera_cfg *cam, const char *fault, const char *state,
                 int64_t since_wall, int64_t at_wall, const char *detail,
                 const uint8_t *roi_jpg, size_t roi_len)
{
    const gateway_cfg *g = app->cfg;
    const char *sev = severity_of(fault);
    char id[37], since[64], at[64];
    util_uuid4(id);
    util_iso8601(since_wall, since, sizeof since);
    util_iso8601(at_wall, at, sizeof at);
    char at_local[64];           /* people read local time; records stay UTC */
    util_local_time(at_wall, at_local, sizeof at_local);

    /* 1. durable record for the backend (sent before PoP records) */
    cJSON *j = cJSON_CreateObject();
    cJSON_AddStringToObject(j, "event_id", id);
    cJSON_AddStringToObject(j, "type", "fault");
    cJSON_AddStringToObject(j, "fault", fault);
    cJSON_AddStringToObject(j, "state", state);
    cJSON_AddStringToObject(j, "severity", sev);
    cJSON_AddStringToObject(j, "site_id", cam->site_id);
    cJSON_AddStringToObject(j, "site_name", cam->name);
    cJSON_AddStringToObject(j, "camera", cam->name);
    cJSON_AddStringToObject(j, "camera_model", cam->model);
    cJSON_AddStringToObject(j, "gateway_id", g->gateway_id);
    cJSON_AddStringToObject(j, "since", since);
    cJSON_AddStringToObject(j, "at", at);
    cJSON_AddStringToObject(j, "detail", detail ? detail : "");
    cJSON_AddStringToObject(j, "app_version", PC_VERSION);
    if (cam->setup) cJSON_AddTrueToObject(j, "setup");
    if (roi_jpg && roi_len) {
        char sha[65];
        sha256_hex(roi_jpg, roi_len, sha);
        cJSON_AddStringToObject(j, "roi_sha256", sha);
    }
    char *json = cJSON_PrintUnformatted(j);
    cJSON_Delete(j);
    if (queue_put(g->queue_dir, QPRIO_ALERT, json, NULL, 0, roi_jpg, roi_len, NULL, 0) != 0)
        LOGE("alerts", "failed to queue alert %s/%s", cam->site_id, fault);
    free(json);
    LOGW(cam->name, "ALERT %s %s: %s", fault, state, detail ? detail : "");

    /* 2. direct notifications, rate limited */
    if (!*g->teams_webhook && !*g->slack_webhook) return;
    /* a site being set up: its region is still moving, so detection alerts are
     * noise; a lost video stream is still worth knowing about */
    if (cam->setup && strcmp(fault, "stream_lost") != 0) return;
    char key[160];
    snprintf(key, sizeof key, "%s/%s", cam->site_id, fault);
    pthread_mutex_lock(&mu);
    int ok = cooldown_ok(key, state, g->alert_cooldown_s, at_wall);
    pthread_mutex_unlock(&mu);
    if (!ok) return;
    if (*g->teams_webhook) direct_push(g->teams_webhook, teams_payload(cam, fault, state, sev, at_local, detail));
    if (*g->slack_webhook) direct_push(g->slack_webhook, slack_payload(cam, fault, state, sev, at_local, detail));
}
