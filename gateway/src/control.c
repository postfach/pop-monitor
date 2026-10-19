#define _GNU_SOURCE
#include "control.h"
#include "alerts.h"
#include "camera.h"
#include "uploader.h"
#include "util.h"
#include "vapix.h"

#include <arpa/inet.h>
#include <cjson/cJSON.h>
#include <errno.h>
#include <math.h>
#include <netinet/in.h>
#include <poll.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>
#include <dirent.h>
#include <ctype.h>

#define MAX_BODY 65536
#define RESTORE_MAX (64 * 1024 * 1024)   /* POST /api/restore: the state files of every site */

static app_ctx *A;
static pthread_t thr;
static int listen_fd = -1;

/* ---------------- helpers ---------------- */


static void set_item(cJSON *obj, const char *key, cJSON *val)
{
    if (cJSON_GetObjectItemCaseSensitive(obj, key)) cJSON_ReplaceItemInObjectCaseSensitive(obj, key, val);
    else cJSON_AddItemToObject(obj, key, val);
}

/* Put the camera's live region into its JSON object (call with cams_mu held). */
static void sync_roi_to_json(int i)
{
    pc_region r;
    float inset;
    status_get_roi(&A->status[i], &r, &inset);
    cJSON *obj = A->cfg->cam_json[i];
    config_region_to_json(obj, &r);
    set_item(obj, "inset_pct", cJSON_CreateNumber(inset));
    A->cfg->cameras[i].region = r;
    A->cfg->cameras[i].inset_pct = inset;
}

/* Save every camera (with its live region) to cameras.json. cams_mu held. */
static void save_all(void)
{
    for (int i = 0; i < A->cfg->n_cameras; i++)
        if (A->cfg->cameras[i].active) sync_roi_to_json(i);
    if (config_save_cameras(A->cfg) == 0 && !A->cfg->cameras_from_state) {
        A->cfg->cameras_from_state = 1;
        LOGI("control", "cameras are now managed from the review page (%s)", A->cfg->cameras_file);
    }
}

static void status_init_from_cfg(int i)
{
    cam_status *s = &A->status[i];
    status_reset(s);
    s->expected_creatives = A->cfg->cameras[i].expected_creatives;
    s->expect_version = 1;
    s->region = A->cfg->cameras[i].region;
    s->inset_pct = A->cfg->cameras[i].inset_pct;
}

/* ---------------- start-up ---------------- */

void control_init_regions(app_ctx *app)
{
    A = app;
    gateway_cfg *g = app->cfg;
    for (int i = 0; i < g->n_cameras; i++)
        if (g->cameras[i].active) status_init_from_cfg(i);
    if (g->cameras_from_state) return;

    /* Region edits saved by the previous version (roi-overrides.json): apply
     * them now; they move into cameras.json at the next save. */
    size_t len;
    char *txt = util_read_file(g->state_file, &len);
    if (!txt) return;
    cJSON *root = cJSON_Parse(txt);
    free(txt);
    const cJSON *cams = root ? cJSON_GetObjectItemCaseSensitive(root, "cameras") : NULL;
    int applied = 0;
    for (int i = 0; cams && i < g->n_cameras; i++) {
        if (!g->cameras[i].active) continue;
        const cJSON *c = cJSON_GetObjectItemCaseSensitive(cams, g->cameras[i].name);
        pc_region r;
        char why[160];
        if (!c || config_parse_region(c, &r) != 0 || !config_region_valid(&r, why, sizeof why))
            continue;
        app->status[i].region = r;
        const cJSON *in = cJSON_GetObjectItemCaseSensitive(c, "inset_pct");
        if (cJSON_IsNumber(in)) app->status[i].inset_pct = (float)in->valuedouble;
        sync_roi_to_json(i);
        applied++;
    }
    cJSON_Delete(root);
    if (applied) LOGI("control", "applied saved region edits for %d camera(s) from %s", applied, g->state_file);
}

/* ---------------- tiny HTTP server ---------------- */

static void send_all(int fd, const void *buf, size_t n)
{
    const char *p = buf;
    while (n) {
        ssize_t w = send(fd, p, n, MSG_NOSIGNAL);
        if (w <= 0) { if (errno == EINTR) continue; return; }
        p += w;
        n -= (size_t)w;
    }
}

static void respond(int fd, int code, const char *ctype, const void *body, size_t n)
{
    const char *reason = code == 200 ? "OK" : code == 201 ? "Created" : code == 400 ? "Bad Request"
                       : code == 202 ? "Accepted" : code == 401 ? "Unauthorized" : code == 404 ? "Not Found" : code == 409 ? "Conflict"
                       : code == 502 ? "Bad Gateway" : "Error";
    char h[256];
    int hl = snprintf(h, sizeof h,
                      "HTTP/1.1 %d %s\r\nContent-Type: %s\r\nContent-Length: %zu\r\n"
                      "Cache-Control: no-store\r\nConnection: close\r\n\r\n", code, reason, ctype, n);
    send_all(fd, h, (size_t)hl);
    if (n) send_all(fd, body, n);
}

static void respond_json(int fd, int code, cJSON *j)
{
    char *s = cJSON_PrintUnformatted(j);
    respond(fd, code, "application/json", s, strlen(s));
    free(s);
    cJSON_Delete(j);
}

static void respond_error(int fd, int code, const char *msg)
{
    cJSON *j = cJSON_CreateObject();
    cJSON_AddStringToObject(j, "error", msg);
    respond_json(fd, code, j);
}

static void url_decode(char *s)
{
    char *o = s;
    for (; *s; s++) {
        if (*s == '%' && s[1] && s[2]) {
            char hx[3] = {s[1], s[2], 0};
            *o++ = (char)strtol(hx, NULL, 16);
            s += 2;
        } else {
            *o++ = *s;
        }
    }
    *o = 0;
}

/* ---------------- camera JSON for the UI ---------------- */

static const char *raw_str(const cJSON *o, const char *k)
{
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(o, k);
    return cJSON_IsString(v) && v->valuestring ? v->valuestring : "";
}

static int raw_int(const cJSON *o, const char *k, int def)
{
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(o, k);
    if (cJSON_IsNumber(v)) return v->valueint;
    if (cJSON_IsBool(v)) return cJSON_IsTrue(v);
    return def;
}

/* Coverage counting state (status mutex held): minutes of data so far, and the
 * last alert-window figure once the window is full. */
static void add_coverage_status(cJSON *j, const camera_cfg *c, const cam_status *s)
{
    int64_t w = util_wall_ms();
    double mins = s->coverage_since_wall ? (w - s->coverage_since_wall) / 60000.0 : -1;
    cJSON_AddNumberToObject(j, "coverage_data_min", mins < 0 ? -1 : (int)mins);
    if (mins >= c->coverage_window_min && s->coverage_window_pct >= 0)
        cJSON_AddNumberToObject(j, "coverage_window_pct", (int)(s->coverage_window_pct + 0.5f));
}

/* One camera for the UI: its own settings (not inherited defaults, never the
 * password) plus live status. cams_mu held. */
static cJSON *camera_json(int i)
{
    const camera_cfg *c = &A->cfg->cameras[i];
    const cJSON *o = A->cfg->cam_json[i];
    cam_status *s = &A->status[i];
    cJSON *j = cJSON_CreateObject();
    cJSON_AddStringToObject(j, "id", c->site_id);          /* key for URLs and the API */
    cJSON_AddStringToObject(j, "name", c->name);           /* what people see          */
    cJSON_AddStringToObject(j, "site_id", c->site_id);
    cJSON_AddStringToObject(j, "display_name", raw_str(o, "name"));
    cJSON_AddStringToObject(j, "model", c->model);
    cJSON_AddStringToObject(j, "host", raw_str(o, "host"));
    cJSON_AddNumberToObject(j, "http_port", raw_int(o, "http_port", 0));
    cJSON_AddNumberToObject(j, "rtsp_port", raw_int(o, "rtsp_port", 0));
    cJSON_AddBoolToObject(j, "use_https", c->use_https);
    cJSON_AddBoolToObject(j, "camera_tls_insecure", c->camera_tls_insecure);
    cJSON_AddStringToObject(j, "rtsp_transport", c->rtsp_transport);
    cJSON_AddStringToObject(j, "video_quality", c->video_quality);
    cJSON_AddStringToObject(j, "stream_params", raw_str(o, "stream_params"));   /* set by hand */
    cJSON_AddStringToObject(j, "stream_effective", c->stream_params);
    cJSON_AddStringToObject(j, "photo_resolution", c->snap.resolution);
    cJSON_AddStringToObject(j, "username", raw_str(o, "username"));
    cJSON_AddBoolToObject(j, "password_set", cJSON_GetObjectItemCaseSensitive(o, "password") != NULL);
    cJSON_AddStringToObject(j, "rtsp_url_override", raw_str(o, "rtsp_url"));
    cJSON_AddStringToObject(j, "vapix_url_override", raw_str(o, "vapix_url"));
    cJSON_AddStringToObject(j, "snapshot_url", c->vapix_url);
    const cJSON *take = cJSON_GetObjectItemCaseSensitive(o, "takeover");
    cJSON_AddBoolToObject(j, "takeover", cJSON_IsTrue(take));
    const cJSON *prof = cJSON_GetObjectItemCaseSensitive(o, "profile");
    cJSON_AddNumberToObject(j, "slot_count", prof ? raw_int(prof, "slot_count", 0) : 0);
    cJSON_AddNumberToObject(j, "slot_ms", prof ? raw_int(prof, "slot_ms", 0) : 0);
    cJSON_AddNumberToObject(j, "board_width", c->board_width);
    cJSON_AddNumberToObject(j, "board_height", c->board_height);
    cJSON_AddNumberToObject(j, "dayparts", c->n_dayparts);
    cJSON_AddStringToObject(j, "operating_hours", c->operating_hours);
    cJSON_AddBoolToObject(j, "ptz_check", c->ptz_check);
    cJSON_AddStringToObject(j, "vault_screen", c->vault_screen);
    cJSON_AddNumberToObject(j, "expected_creatives", c->expected_creatives);
    cJSON_AddBoolToObject(j, "setup", c->setup);
    cJSON_AddNumberToObject(j, "coverage_min_pct", c->coverage_min_pct);
    cJSON_AddNumberToObject(j, "coverage_window_min", c->coverage_window_min);
    cJSON_AddBoolToObject(j, "coverage_custom", cJSON_GetObjectItemCaseSensitive(o, "coverage_min_pct") ||
                                                cJSON_GetObjectItemCaseSensitive(o, "coverage_window_min"));
    pthread_mutex_lock(&s->mu);
    add_coverage_status(j, c, s);
    cJSON_AddNumberToObject(j, "uptime_pct", (int)(status_uptime(s, util_wall_ms()) * 1000 + 0.5) / 10.0);
    cJSON_AddNumberToObject(j, "reconnects", s->reconnects);
    if (s->last_frame_wall) cJSON_AddNumberToObject(j, "last_frame_age_s", (double)((util_wall_ms() - s->last_frame_wall) / 1000));
    cJSON_AddBoolToObject(j, "connected", s->connected);
    cJSON_AddStringToObject(j, "state", pc_state_name(s->state));
    cJSON_AddNumberToObject(j, "plays_last_hour", status_plays_last_hour(s, util_wall_ms()));
    cJSON_AddNumberToObject(j, "expected_per_hour", s->expected_per_hour);
    if (s->expected_per_hour > 0) {
        double sl = status_slots_credited(s, util_wall_ms(), 3600000);
        cJSON_AddNumberToObject(j, "slots_last_hour", (int)(sl + 0.5));
        cJSON_AddNumberToObject(j, "coverage", sl / s->expected_per_hour);
    }
    if (s->last_play_wall) {
        char ts[64];
        util_iso8601(s->last_play_wall, ts, sizeof ts);
        cJSON_AddStringToObject(j, "last_play_at", ts);
    }
    cJSON *f = cJSON_AddArrayToObject(j, "active_faults");
    for (int k = 0; k < PC_FAULT_COUNT; k++)
        if (s->fault_active[k]) cJSON_AddItemToArray(f, cJSON_CreateString(pc_fault_name(k)));
    if (s->stream_lost) cJSON_AddItemToArray(f, cJSON_CreateString("stream_lost"));
    if (s->camera_moved) cJSON_AddItemToArray(f, cJSON_CreateString("camera_moved"));
    if (s->low_coverage) cJSON_AddItemToArray(f, cJSON_CreateString("low_coverage"));
    config_region_to_json(j, &s->region);
    cJSON_AddNumberToObject(j, "inset_pct", s->inset_pct);
    pthread_mutex_unlock(&s->mu);
    return j;
}

static void handle_list(int fd)
{
    cJSON *arr = cJSON_CreateArray();
    pthread_mutex_lock(&A->cams_mu);
    for (int i = 0; i < A->cfg->n_cameras; i++)
        if (A->cfg->cameras[i].active) cJSON_AddItemToArray(arr, camera_json(i));
    pthread_mutex_unlock(&A->cams_mu);
    respond_json(fd, 200, arr);
}

static void handle_settings(int fd)
{
    const cJSON *d = A->cfg->defaults_json;
    cJSON *j = cJSON_CreateObject();
    cJSON_AddStringToObject(j, "gateway_id", A->cfg->gateway_id);
    cJSON_AddStringToObject(j, "default_username", raw_str(d, "username"));
    cJSON_AddBoolToObject(j, "default_password_set", cJSON_GetObjectItemCaseSensitive(d, "password") != NULL);
    cJSON_AddBoolToObject(j, "cameras_from_state", A->cfg->cameras_from_state);
    cJSON_AddStringToObject(j, "cameras_file", A->cfg->cameras_file);
    cJSON_AddNumberToObject(j, "max_cameras", MAX_CAMERAS);
    cJSON_AddNumberToObject(j, "default_coverage_min_pct", raw_int(d, "coverage_min_pct", 90));
    cJSON_AddNumberToObject(j, "default_coverage_window_min", raw_int(d, "coverage_window_min", 30));
    respond_json(fd, 200, j);
}

/* Merge the UI's form into a camera's JSON object. Only the fields the UI
 * edits are touched; anything else (detector tuning, dayparts, region) stays.
 * An empty value removes the key so the default applies. */
static void merge_form(cJSON *obj, const cJSON *in)
{
    static const char *strs[] = {"site_id", "model", "host", "username", "rtsp_transport",
                                 "operating_hours", "vault_screen", "video_quality", "stream_params", NULL};
    for (int k = 0; strs[k]; k++) {
        const cJSON *v = cJSON_GetObjectItemCaseSensitive(in, strs[k]);
        if (!cJSON_IsString(v)) continue;
        if (*v->valuestring) set_item(obj, strs[k], cJSON_CreateString(v->valuestring));
        else cJSON_DeleteItemFromObjectCaseSensitive(obj, strs[k]);
    }
    /* display name maps to "name"; the URL overrides map to rtsp_url / vapix_url */
    static const char *map[][2] = {{"display_name", "name"}, {"rtsp_url_override", "rtsp_url"},
                                   {"vapix_url_override", "vapix_url"}};
    for (int k = 0; k < 3; k++) {
        const cJSON *v = cJSON_GetObjectItemCaseSensitive(in, map[k][0]);
        if (!cJSON_IsString(v)) continue;
        if (*v->valuestring) set_item(obj, map[k][1], cJSON_CreateString(v->valuestring));
        else cJSON_DeleteItemFromObjectCaseSensitive(obj, map[k][1]);
    }
    const cJSON *pw = cJSON_GetObjectItemCaseSensitive(in, "password");
    if (cJSON_IsString(pw) && *pw->valuestring) set_item(obj, "password", cJSON_CreateString(pw->valuestring));
    if (cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(in, "clear_password")))
        cJSON_DeleteItemFromObjectCaseSensitive(obj, "password");
    static const char *ints[] = {"http_port", "rtsp_port", "board_width", "board_height", NULL};
    for (int k = 0; ints[k]; k++) {
        const cJSON *v = cJSON_GetObjectItemCaseSensitive(in, ints[k]);
        if (!v || cJSON_IsNull(v) || (cJSON_IsNumber(v) && v->valueint == 0))
            { if (v) cJSON_DeleteItemFromObjectCaseSensitive(obj, ints[k]); continue; }
        if (cJSON_IsNumber(v)) set_item(obj, ints[k], cJSON_CreateNumber(v->valueint));
    }
    static const char *cov[] = {"coverage_min_pct", "coverage_window_min", NULL};
    for (int k = 0; cov[k]; k++) {
        const cJSON *v = cJSON_GetObjectItemCaseSensitive(in, cov[k]);
        if (!v) continue;
        if (cJSON_IsNumber(v)) set_item(obj, cov[k], cJSON_CreateNumber(v->valueint));
        else cJSON_DeleteItemFromObjectCaseSensitive(obj, cov[k]);   /* null or "": use the default */
    }
    static const char *bools[] = {"use_https", "camera_tls_insecure", "ptz_check", "takeover", NULL};
    for (int k = 0; bools[k]; k++) {
        const cJSON *v = cJSON_GetObjectItemCaseSensitive(in, bools[k]);
        if (cJSON_IsBool(v)) set_item(obj, bools[k], cJSON_CreateBool(cJSON_IsTrue(v)));
    }
    const cJSON *sc = cJSON_GetObjectItemCaseSensitive(in, "slot_count");
    const cJSON *sm = cJSON_GetObjectItemCaseSensitive(in, "slot_ms");
    if (cJSON_IsNumber(sc) || cJSON_IsNumber(sm)) {
        int n = cJSON_IsNumber(sc) ? sc->valueint : 0, ms = cJSON_IsNumber(sm) ? sm->valueint : 0;
        if (n <= 0 && ms <= 0) {
            cJSON_DeleteItemFromObjectCaseSensitive(obj, "profile");
        } else {
            cJSON *p = cJSON_CreateObject();
            cJSON_AddNumberToObject(p, "slot_count", n > 0 ? n : 0);
            cJSON_AddNumberToObject(p, "slot_ms", ms > 0 ? ms : 0);
            set_item(obj, "profile", p);
        }
    }
}

/* Validate a camera object for slot `self` (-1 for a new camera). */
static int check_camera(const cJSON *obj, int self, camera_cfg *out, char *err, size_t n)
{
    if (config_build_camera(A->cfg, obj, out, err, n) != 0) return -1;
    for (int i = 0; i < A->cfg->n_cameras; i++) {
        if (i == self || !A->cfg->cameras[i].active) continue;
        if (!strcmp(A->cfg->cameras[i].site_id, out->site_id)) {
            snprintf(err, n, "site ID '%s' is already used by another site", out->site_id);
            return -1;
        }
    }
    return 0;
}

static void handle_create(int fd, const char *body, int dry_run)
{
    cJSON *in = cJSON_Parse(body);
    if (!cJSON_IsObject(in)) { cJSON_Delete(in); respond_error(fd, 400, "expected a JSON object"); return; }
    cJSON *obj = cJSON_CreateObject();
    merge_form(obj, in);
    /* a new site starts in setup (unless asked otherwise) until its region fits */
    cJSON_AddBoolToObject(obj, "setup", !cJSON_IsFalse(cJSON_GetObjectItemCaseSensitive(in, "setup")));
    pc_region r;
    char rwhy[160];
    if (config_parse_region(in, &r) != 0 || !config_region_valid(&r, rwhy, sizeof rwhy)) {
        pc_point full[4] = {{0, 0}, {1, 0}, {1, 1}, {0, 1}};   /* set it on the region page next */
        pc_region_from_corners(&r, full);
    }
    config_region_to_json(obj, &r);
    cJSON_Delete(in);

    camera_cfg c;
    char err[300];
    pthread_mutex_lock(&A->cams_mu);
    int slot = -1;
    for (int i = 0; i < A->cfg->n_cameras; i++)
        if (!A->cfg->cameras[i].active) { slot = i; break; }
    if (slot < 0 && A->cfg->n_cameras < MAX_CAMERAS) slot = A->cfg->n_cameras;
    if (slot < 0) {
        pthread_mutex_unlock(&A->cams_mu);
        cJSON_Delete(obj);
        respond_error(fd, 400, "the gateway is at its camera limit");
        return;
    }
    if (check_camera(obj, -1, &c, err, sizeof err) != 0) {
        pthread_mutex_unlock(&A->cams_mu);
        cJSON_Delete(obj);
        respond_error(fd, 400, err);
        return;
    }
    if (dry_run) {                               /* bulk import preview: validate only */
        pthread_mutex_unlock(&A->cams_mu);
        cJSON_Delete(obj);
        cJSON *ok = cJSON_CreateObject();
        cJSON_AddStringToObject(ok, "status", "valid");
        cJSON_AddStringToObject(ok, "site_id", c.site_id);
        cJSON_AddStringToObject(ok, "name", c.name);
        cJSON_AddStringToObject(ok, "video_url", c.rtsp_url);
        respond_json(fd, 200, ok);
        return;
    }
    c.active = 1;
    A->cfg->cameras[slot] = c;
    cJSON_Delete(A->cfg->cam_json[slot]);
    A->cfg->cam_json[slot] = obj;
    if (slot >= A->cfg->n_cameras) A->cfg->n_cameras = slot + 1;
    status_init_from_cfg(slot);
    camera_start(A, slot);
    save_all();
    cJSON *j = camera_json(slot);
    pthread_mutex_unlock(&A->cams_mu);
    LOGI("control", "site %s added from the review page", c.site_id);
    respond_json(fd, 201, j);
}

/* Close the open faults of a camera that is being restarted or removed, so the
 * backend and Teams don't keep showing alerts nobody will ever clear. Call
 * after camera_stop() and before its config or status is replaced. */
static void clear_open_faults(int idx, const char *why)
{
    cam_status *s = &A->status[idx];
    const char *open[PC_FAULT_COUNT + 3];
    int n = 0;
    pthread_mutex_lock(&s->mu);
    for (int k = 0; k < PC_FAULT_COUNT; k++)
        if (s->fault_active[k]) open[n++] = pc_fault_name(k);
    if (s->stream_lost) open[n++] = "stream_lost";
    if (s->camera_moved) open[n++] = "camera_moved";
    if (s->low_coverage) open[n++] = "low_coverage";
    pthread_mutex_unlock(&s->mu);
    int64_t w = util_wall_ms();
    for (int i = 0; i < n; i++)
        alerts_send(A, &A->cfg->cameras[idx], open[i], "cleared", w, w, why, NULL, 0);
}

static void handle_update(int fd, int idx, const char *body)
{
    cJSON *in = cJSON_Parse(body);
    if (!cJSON_IsObject(in)) { cJSON_Delete(in); respond_error(fd, 400, "expected a JSON object"); return; }
    camera_cfg c;
    char err[300];
    pthread_mutex_lock(&A->cams_mu);
    sync_roi_to_json(idx);                       /* keep the live region */
    cJSON *obj = cJSON_Duplicate(A->cfg->cam_json[idx], 1);
    merge_form(obj, in);
    cJSON_Delete(in);
    if (check_camera(obj, idx, &c, err, sizeof err) != 0) {
        pthread_mutex_unlock(&A->cams_mu);
        cJSON_Delete(obj);
        respond_error(fd, 400, err);
        return;
    }
    char old[64];
    snprintf(old, sizeof old, "%s", A->cfg->cameras[idx].site_id);
    camera_stop(idx);                            /* restart this camera only */
    clear_open_faults(idx, "site settings changed; checking again from now");
    if (strcmp(old, c.site_id)) {                /* site ID renamed: keep its creative library */
        char from[700], to[700];
        config_library_path(A->cfg, old, from, sizeof from);
        config_library_path(A->cfg, c.site_id, to, sizeof to);
        rename(from, to);
    }
    c.active = 1;
    A->cfg->cameras[idx] = c;
    cJSON_Delete(A->cfg->cam_json[idx]);
    A->cfg->cam_json[idx] = obj;
    status_init_from_cfg(idx);
    camera_start(A, idx);
    save_all();
    cJSON *j = camera_json(idx);
    pthread_mutex_unlock(&A->cams_mu);
    LOGI("control", "site %s updated from the review page%s%s%s", c.site_id, strcmp(old, c.site_id) ? " (was " : "",
         strcmp(old, c.site_id) ? old : "", strcmp(old, c.site_id) ? ")" : "");
    respond_json(fd, 200, j);
}

static void handle_delete(int fd, int idx)
{
    pthread_mutex_lock(&A->cams_mu);
    char name[64];
    snprintf(name, sizeof name, "%s", A->cfg->cameras[idx].site_id);
    camera_stop(idx);
    clear_open_faults(idx, "site removed");
    A->cfg->cameras[idx].active = 0;
    cJSON_Delete(A->cfg->cam_json[idx]);
    A->cfg->cam_json[idx] = NULL;
    status_reset(&A->status[idx]);
    save_all();
    pthread_mutex_unlock(&A->cams_mu);
    LOGI("control", "site %s removed from the review page", name);
    cJSON *ok = cJSON_CreateObject();
    cJSON_AddStringToObject(ok, "status", "removed");
    respond_json(fd, 200, ok);
}

static void handle_snapshot(int fd, int idx)
{
    pthread_mutex_lock(&A->cams_mu);
    camera_cfg c = A->cfg->cameras[idx];        /* copy: the slot may change while we wait */
    pthread_mutex_unlock(&A->cams_mu);
    vapix vx;
    if (vapix_init(&vx, c.vapix_url, c.username, c.password, c.camera_tls_insecure || A->cfg->tls_insecure) != 0) {
        respond_error(fd, 500, "curl init failed");
        return;
    }
    char q[96];
    snprintf(q, sizeof q, "compression=25%s%s", *c.snap.resolution ? "&resolution=" : "", c.snap.resolution);
    uint8_t *jpg;
    size_t len;
    if (vapix_jpeg(&vx, q, &jpg, &len) != 0) {
        vapix_free(&vx);
        char msg[400];
        snprintf(msg, sizeof msg, "could not get a snapshot from the camera at %s (check host, web port, "
                 "HTTPS setting and the camera user's password)", c.vapix_url);
        respond_error(fd, 502, msg);
        return;
    }
    vapix_free(&vx);
    respond(fd, 200, "image/jpeg", jpg, len);
    free(jpg);
}

static void handle_set_roi(int fd, int idx, const char *body)
{
    cJSON *j = cJSON_Parse(body);
    pc_region r;
    char why[160];
    if (!j || config_parse_region(j, &r) != 0) {
        cJSON_Delete(j);
        respond_error(fd, 400, "expected {\"roi\": [[x,y],[x,y],[x,y],[x,y]], \"inset_pct\": n, "
                               "\"roi_shape\": \"standard\"|\"curve\"|\"fold\", \"roi_curve\": [8 points], "
                               "\"roi_fold\": [2 points], \"fold_pct\": n}");
        return;
    }
    if (!config_region_valid(&r, why, sizeof why)) {
        cJSON_Delete(j);
        respond_error(fd, 400, why);
        return;
    }
    pthread_mutex_lock(&A->cams_mu);
    cam_status *s = &A->status[idx];
    const cJSON *in = cJSON_GetObjectItemCaseSensitive(j, "inset_pct");
    pthread_mutex_lock(&s->mu);
    s->region = r;
    if (cJSON_IsNumber(in) && in->valuedouble >= 0 && in->valuedouble <= 20) s->inset_pct = (float)in->valuedouble;
    s->roi_version++;
    pthread_mutex_unlock(&s->mu);
    save_all();
    LOGI(A->cfg->cameras[idx].name, "region changed from the review page");
    pthread_mutex_unlock(&A->cams_mu);
    cJSON_Delete(j);
    cJSON *ok = cJSON_CreateObject();
    cJSON_AddStringToObject(ok, "status", "applied");
    respond_json(fd, 200, ok);
}

/* Booking-derived expectations, set by the receiver from Vault. Applied live:
 * the camera is not restarted. Body: {"expected_creatives": n} (0 = auto). */
static void handle_expectations(int fd, int idx, const char *body)
{
    cJSON *in = cJSON_Parse(body);
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(in, "expected_creatives");
    if (!cJSON_IsNumber(v) || v->valueint < 0 || v->valueint > 200) {
        cJSON_Delete(in);
        respond_error(fd, 400, "expected_creatives must be 0-200");
        return;
    }
    int n = v->valueint;
    cJSON_Delete(in);
    pthread_mutex_lock(&A->cams_mu);
    camera_cfg *c = &A->cfg->cameras[idx];
    int changed = c->expected_creatives != n;
    c->expected_creatives = n;
    if (n) set_item(A->cfg->cam_json[idx], "expected_creatives", cJSON_CreateNumber(n));
    else cJSON_DeleteItemFromObjectCaseSensitive(A->cfg->cam_json[idx], "expected_creatives");
    cam_status *s = &A->status[idx];
    pthread_mutex_lock(&s->mu);
    s->expected_creatives = n;
    s->expect_version++;
    pthread_mutex_unlock(&s->mu);
    if (changed) {
        save_all();
        LOGI(c->name, "expecting %d distinct creatives per loop%s", n, n ? " (from bookings)" : " (worked out from the loop)");
    }
    pthread_mutex_unlock(&A->cams_mu);
    cJSON *ok = cJSON_CreateObject();
    cJSON_AddNumberToObject(ok, "expected_creatives", n);
    respond_json(fd, 200, ok);
}

/* ---------------------------------------------------------------- backup and restore
 * The gateway's state, for the receiver's Backup: the sites (cameras.json, with the live
 * regions), each site's learned creatives and its photo history. Restore stages the
 * same files and restarts; they are put in place before anything loads
 * (config_apply_staged_restore), so nothing running writes over them. */
static void add_dir_files(cJSON *files, const char *sub)
{
    char dir[700], p[1000];
    snprintf(dir, sizeof dir, "%s/%s", A->cfg->state_dir, sub);
    DIR *d = opendir(dir);
    struct dirent *e;
    while (d && (e = readdir(d))) {
        size_t L = strlen(e->d_name);
        if (e->d_name[0] == '.' || L < 6 || strcmp(e->d_name + L - 5, ".json")) continue;
        snprintf(p, sizeof p, "%s/%s", dir, e->d_name);
        size_t len;
        char *txt = util_read_file(p, &len);
        if (!txt) continue;
        char key[400];
        snprintf(key, sizeof key, "%s/%s", sub, e->d_name);
        cJSON_AddItemToObject(files, key, cJSON_CreateString(txt));
        free(txt);
    }
    if (d) closedir(d);
}

static void handle_backup(int fd)
{
    pthread_mutex_lock(&A->cams_mu);
    for (int i = 0; i < A->cfg->n_cameras; i++)
        if (A->cfg->cameras[i].active) sync_roi_to_json(i);
    save_all();
    pthread_mutex_unlock(&A->cams_mu);
    cJSON *j = cJSON_CreateObject();
    cJSON_AddStringToObject(j, "format", "pop-gateway/state");
    cJSON_AddNumberToObject(j, "version", 1);
    cJSON_AddStringToObject(j, "gateway_id", A->cfg->gateway_id);
    cJSON_AddStringToObject(j, "app_version", PC_VERSION);
    cJSON *files = cJSON_AddObjectToObject(j, "files");
    size_t len;
    char *txt = util_read_file(A->cfg->cameras_file, &len);
    if (txt) { cJSON_AddItemToObject(files, "cameras.json", cJSON_CreateString(txt)); free(txt); }
    add_dir_files(files, "creatives");
    add_dir_files(files, "photos");
    respond_json(fd, 200, j);
}

static int restore_name_ok(const char *k)
{
    if (!strcmp(k, "cameras.json")) return 1;
    const char *rest = !strncmp(k, "creatives/", 10) ? k + 10 : !strncmp(k, "photos/", 7) ? k + 7 : NULL;
    if (!rest || !*rest) return 0;
    size_t L = strlen(rest);
    if (L < 6 || L > 120 || strcmp(rest + L - 5, ".json")) return 0;
    for (size_t i = 0; i < L - 5; i++)
        if (!isalnum((unsigned char)rest[i]) && rest[i] != '-' && rest[i] != '_') return 0;
    return 1;
}

static void handle_restore(int fd, const char *body)
{
    cJSON *in = cJSON_Parse(body);
    const cJSON *files = cJSON_GetObjectItemCaseSensitive(in, "files");
    const cJSON *fmt = cJSON_GetObjectItemCaseSensitive(in, "format");
    if (!cJSON_IsObject(files) || !cJSON_IsString(fmt) || strcmp(fmt->valuestring, "pop-gateway/state")) {
        cJSON_Delete(in);
        respond_error(fd, 400, "expected a gateway state from a backup");
        return;
    }
    const cJSON *f;
    cJSON_ArrayForEach(f, files) {
        if (!restore_name_ok(f->string) || !cJSON_IsString(f)) {
            cJSON_Delete(in);
            respond_error(fd, 400, "unexpected file in the backup");
            return;
        }
    }
    const cJSON *cams = cJSON_GetObjectItemCaseSensitive(files, "cameras.json");
    cJSON *chk = cams ? cJSON_Parse(cams->valuestring) : NULL;
    int ok = cJSON_IsArray(chk);
    cJSON_Delete(chk);
    if (!ok) {
        cJSON_Delete(in);
        respond_error(fd, 400, "the backup has no sites (cameras.json)");
        return;
    }
    char staged[700], p[1000];
    snprintf(staged, sizeof staged, "%s/restore-staged", A->cfg->state_dir);
    snprintf(p, sizeof p, "%s/creatives", staged);
    util_mkdir_p(p);
    snprintf(p, sizeof p, "%s/photos", staged);
    util_mkdir_p(p);
    int n = 0;
    cJSON_ArrayForEach(f, files) {
        snprintf(p, sizeof p, "%s/%s", staged, f->string);
        if (util_write_file_atomic(p, f->valuestring, strlen(f->valuestring)) != 0) {
            cJSON_Delete(in);
            respond_error(fd, 500, "cannot write the restore");
            return;
        }
        n++;
    }
    cJSON_Delete(in);
    snprintf(p, sizeof p, "%s/READY", staged);
    util_write_file_atomic(p, "1", 1);
    LOGW("control", "restore staged (%d file(s)): restarting to apply it", n);
    cJSON *ok_j = cJSON_CreateObject();
    cJSON_AddBoolToObject(ok_j, "staged", 1);
    cJSON_AddNumberToObject(ok_j, "files", n);
    respond_json(fd, 202, ok_j);
    A->exit_code = 3;          /* the service manager starts it again, and it is applied first */
    A->running = 0;
}

/* Setup mode on or off: {"setup": true|false}. No restart: only alerts and the
 * receiver's counting depend on it. */
static void handle_set_setup(int fd, int idx, const char *body)
{
    cJSON *in = cJSON_Parse(body && *body ? body : "{}");
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(in, "setup");
    if (!cJSON_IsBool(v)) { cJSON_Delete(in); respond_error(fd, 400, "expected {\"setup\": true|false}"); return; }
    int on = cJSON_IsTrue(v);
    cJSON_Delete(in);
    pthread_mutex_lock(&A->cams_mu);
    A->cfg->cameras[idx].setup = on;
    set_item(A->cfg->cam_json[idx], "setup", cJSON_CreateBool(on));
    save_all();
    cJSON *j = camera_json(idx);
    pthread_mutex_unlock(&A->cams_mu);
    LOGI(A->cfg->cameras[idx].name, "%s", on ? "in setup: detection alerts held back" : "live");
    respond_json(fd, 200, j);
}

/* Forget every creative a site has seen (testing, or a complete change of
 * campaigns). Body: {"restart_numbering": true} also starts IDs again at
 * c-0001; otherwise new creatives continue after the highest ID used, so
 * they can't be confused with earlier records. */
static void handle_reset_creatives(int fd, int idx, const char *body)
{
    cJSON *in = cJSON_Parse(body && *body ? body : "{}");
    int renumber = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(in, "restart_numbering"));
    cJSON_Delete(in);
    pthread_mutex_lock(&A->cams_mu);
    cam_status *s = &A->status[idx];
    pthread_mutex_lock(&s->mu);
    s->reset_renumber = renumber;
    s->reset_version++;
    pthread_mutex_unlock(&s->mu);
    LOGI(A->cfg->cameras[idx].name, "resetting creatives%s", renumber ? " and restarting numbering" : "");
    /* the detection faults start over with the new library; video and PTZ
     * faults are about the camera and stay as they are */
    const char *open[PC_FAULT_COUNT + 1];
    int n = 0;
    pthread_mutex_lock(&s->mu);
    for (int k = 0; k < PC_FAULT_COUNT; k++)
        if (s->fault_active[k]) { open[n++] = pc_fault_name(k); s->fault_active[k] = 0; }
    if (s->low_coverage) { open[n++] = "low_coverage"; s->low_coverage = 0; }
    pthread_mutex_unlock(&s->mu);
    int64_t w = util_wall_ms();
    for (int i = 0; i < n; i++)
        alerts_send(A, &A->cfg->cameras[idx], open[i], "cleared", w, w,
                    "creatives reset; checking again from now", NULL, 0);
    pthread_mutex_unlock(&A->cams_mu);
    cJSON *ok = cJSON_CreateObject();
    cJSON_AddBoolToObject(ok, "reset", 1);
    cJSON_AddBoolToObject(ok, "restart_numbering", renumber);
    respond_json(fd, 200, ok);
}

static int creative_num(const cJSON *v)
{
    int n = 0;
    if (cJSON_IsNumber(v)) return v->valueint;
    if (cJSON_IsString(v) && (sscanf(v->valuestring, "c-%d", &n) == 1 || sscanf(v->valuestring, "%d", &n) == 1)) return n;
    return 0;
}

/* Merge creative IDs that are the same artwork (the receiver finds these by
 * matching PoP images to the booked artwork). Body:
 * {"merges": [{"from": "c-0042", "into": "c-0005"}, ...]}. Applied by the
 * camera thread: from-IDs become extra looks of the into-ID. */
static void handle_merge_creatives(int fd, int idx, const char *body)
{
    cJSON *in = cJSON_Parse(body && *body ? body : "{}");
    const cJSON *ms = cJSON_GetObjectItemCaseSensitive(in, "merges"), *m;
    if (!cJSON_IsArray(ms)) {
        cJSON_Delete(in);
        respond_error(fd, 400, "expected {\"merges\": [{\"from\": \"c-0042\", \"into\": \"c-0005\"}]}");
        return;
    }
    cam_status *s = &A->status[idx];
    int queued = 0, bad = 0;
    pthread_mutex_lock(&s->mu);
    cJSON_ArrayForEach(m, ms) {
        int f = creative_num(cJSON_GetObjectItemCaseSensitive(m, "from"));
        int t = creative_num(cJSON_GetObjectItemCaseSensitive(m, "into"));
        if (f <= 0 || t <= 0 || f == t || s->nmerge >= MERGEQ) { bad++; continue; }
        s->merge_from[s->nmerge] = f;
        s->merge_into[s->nmerge] = t;
        s->nmerge++;
        queued++;
    }
    pthread_mutex_unlock(&s->mu);
    cJSON_Delete(in);
    if (queued) LOGI(A->cfg->cameras[idx].name, "%d creative merge(s) queued", queued);
    cJSON *ok = cJSON_CreateObject();
    cJSON_AddNumberToObject(ok, "queued", queued);
    cJSON_AddNumberToObject(ok, "rejected", bad);
    respond_json(fd, 200, ok);
}

/* Undo a merge that was wrong (two different artworks). Body:
 * {"from": "c-0042", "into": "c-0005", "with": ["c-0040"]}: the looks c-0005
 * took from c-0042 (and from the IDs in "with", merged into c-0042 before)
 * become c-0042 again. Applied by the camera thread. */
static void handle_split_creative(int fd, int idx, const char *body)
{
    cJSON *in = cJSON_Parse(body && *body ? body : "{}");
    split_req r = {0};
    r.from = creative_num(cJSON_GetObjectItemCaseSensitive(in, "from"));
    r.into = creative_num(cJSON_GetObjectItemCaseSensitive(in, "into"));
    const cJSON *w = cJSON_GetObjectItemCaseSensitive(in, "with"), *x;
    cJSON_ArrayForEach(x, w) {
        int n = creative_num(x);
        if (n > 0 && r.nalso < SPLIT_ALSO) r.also[r.nalso++] = n;
    }
    cJSON_Delete(in);
    if (r.from <= 0 || r.into <= 0 || r.from == r.into) {
        respond_error(fd, 400, "expected {\"from\": \"c-0042\", \"into\": \"c-0005\"}");
        return;
    }
    cam_status *s = &A->status[idx];
    pthread_mutex_lock(&s->mu);
    int ok = s->nsplit < SPLITQ;
    if (ok) s->split[s->nsplit++] = r;
    pthread_mutex_unlock(&s->mu);
    if (!ok) { respond_error(fd, 503, "too many waiting; try again in a moment"); return; }
    LOGI(A->cfg->cameras[idx].name, "separating c-%04d from c-%04d (queued)", r.from, r.into);
    cJSON *j = cJSON_CreateObject();
    cJSON_AddBoolToObject(j, "queued", 1);
    respond_json(fd, 200, j);
}

static void handle(int fd)
{
    struct timeval tv = {10, 0};
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
    /* headers (up to 16 KB) then a body of up to MAX_BODY */
    size_t cap = 16384 + MAX_BODY;
    char *buf = malloc(cap + MAX_BODY + 1);
    if (!buf) return;
    buf[0] = 0;
    size_t n = 0;
    char *hdr_end = NULL;
    while (n < cap && !(hdr_end = strstr(buf, "\r\n\r\n"))) {
        ssize_t r = recv(fd, buf + n, cap - n, 0);
        if (r <= 0) { free(buf); return; }
        n += (size_t)r;
        buf[n] = 0;
    }
    if (!hdr_end) { free(buf); return; }
    *hdr_end = 0;
    char *body = hdr_end + 4;
    size_t have = n - (size_t)(body - buf);

    char method[8] = "", path[512] = "";
    if (sscanf(buf, "%7s %511s", method, path) != 2) { free(buf); return; }
    size_t clen = 0;
    const char *auth = NULL;
    for (char *line = strstr(buf, "\r\n"); line; line = strstr(line + 2, "\r\n")) {
        char *l = line + 2;
        if (!strncasecmp(l, "Content-Length:", 15)) clen = strtoul(l + 15, NULL, 10);
        else if (!strncasecmp(l, "Authorization:", 14)) auth = l + 14;
    }
    char want[300];
    snprintf(want, sizeof want, "Bearer %s", A->cfg->api_key);
    while (auth && *auth == ' ') auth++;
    size_t wl = strlen(want);
    if (!auth || strncmp(auth, want, wl) != 0 || (auth[wl] != '\r' && auth[wl] != 0)) {
        respond_error(fd, 401, "missing or wrong API key");
        free(buf);
        return;
    }
    char *big = NULL;            /* a restore is larger than any other request */
    if (clen > MAX_BODY && !strcmp(method, "POST") && !strncmp(path, "/api/restore", 12)) {
        if (clen > RESTORE_MAX || !(big = malloc(clen + 1))) {
            respond_error(fd, 400, "restore too large");
            free(buf);
            return;
        }
        memcpy(big, body, have);
        struct timeval tv2 = {60, 0};
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv2, sizeof tv2);
        while (have < clen) {
            ssize_t r = recv(fd, big + have, clen - have, 0);
            if (r <= 0) break;
            have += (size_t)r;
        }
        big[have] = 0;
        body = big;
    } else {
        while (have < clen && have < MAX_BODY) {
            ssize_t r = recv(fd, body + have, MAX_BODY - have, 0);
            if (r <= 0) break;
            have += (size_t)r;
        }
        body[have < MAX_BODY ? have : MAX_BODY] = 0;
    }

    char *q = strchr(path, '?');
    int dry_run = q && strstr(q, "dry_run=1") != NULL;
    if (q) *q = 0;
    if (!strcmp(path, "/api/cameras")) {
        if (!strcmp(method, "GET")) handle_list(fd);
        else if (!strcmp(method, "POST")) handle_create(fd, body, dry_run);
        else respond_error(fd, 404, "unknown endpoint");
    } else if (!strcmp(method, "GET") && !strcmp(path, "/api/settings")) {
        handle_settings(fd);
    } else if (!strcmp(method, "GET") && !strcmp(path, "/api/backup")) {
        handle_backup(fd);
    } else if (!strcmp(method, "POST") && !strcmp(path, "/api/restore")) {
        if (big && have < clen) respond_error(fd, 400, "the restore didn't arrive complete");
        else handle_restore(fd, body);
    } else if (!strcmp(method, "GET") && !strcmp(path, "/api/health")) {
        cJSON *h = gateway_health_json();
        cJSON_AddStringToObject(h, "gateway_id", A->cfg->gateway_id);
        respond_json(fd, 200, h);
    } else if (!strncmp(path, "/api/cameras/", 13)) {
        char name[512];
        snprintf(name, sizeof name, "%s", path + 13);
        char *slash = strrchr(name, '/');
        const char *action = "";
        if (slash) { *slash = 0; action = slash + 1; }
        url_decode(name);
        pthread_mutex_lock(&A->cams_mu);
        int idx = config_find_camera(A->cfg, name);
        pthread_mutex_unlock(&A->cams_mu);
        if (idx < 0) respond_error(fd, 404, "no such site");
        else if (!strcmp(method, "GET") && !strcmp(action, "snapshot")) handle_snapshot(fd, idx);
        else if ((!strcmp(method, "PUT") || !strcmp(method, "POST")) && !strcmp(action, "roi"))
            handle_set_roi(fd, idx, body);
        else if (!strcmp(method, "PUT") && !strcmp(action, "expectations")) handle_expectations(fd, idx, body);
        else if (!strcmp(method, "POST") && !strcmp(action, "reset-creatives")) handle_reset_creatives(fd, idx, body);
        else if (!strcmp(method, "POST") && !strcmp(action, "setup")) handle_set_setup(fd, idx, body);
        else if (!strcmp(method, "POST") && !strcmp(action, "merge-creatives")) handle_merge_creatives(fd, idx, body);
        else if (!strcmp(method, "POST") && !strcmp(action, "split-creative")) handle_split_creative(fd, idx, body);
        else if (!*action && !strcmp(method, "PUT")) handle_update(fd, idx, body);
        else if (!*action && !strcmp(method, "DELETE")) handle_delete(fd, idx);
        else if (!*action && !strcmp(method, "GET")) {
            pthread_mutex_lock(&A->cams_mu);
            cJSON *j = camera_json(idx);
            pthread_mutex_unlock(&A->cams_mu);
            respond_json(fd, 200, j);
        } else respond_error(fd, 404, "unknown endpoint");
    } else if (!strcmp(path, "/healthz")) {
        respond(fd, 200, "text/plain", "ok", 2);
    } else {
        respond_error(fd, 404, "unknown endpoint");
    }
    free(big);
    free(buf);
}

static void *server_thread(void *arg)
{
    (void)arg;
    while (A->running) {
        struct pollfd p = {listen_fd, POLLIN, 0};
        if (poll(&p, 1, 500) <= 0) continue;
        int fd = accept(listen_fd, NULL, NULL);
        if (fd < 0) continue;
        handle(fd);
        close(fd);
    }
    return NULL;
}

int control_start(app_ctx *app)
{
    A = app;
    if (app->cfg->control_port <= 0) return 0;
    listen_fd = socket(AF_INET, SOCK_STREAM, 0);
    int one = 1;
    setsockopt(listen_fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    struct sockaddr_in a;
    memset(&a, 0, sizeof a);
    a.sin_family = AF_INET;
    a.sin_port = htons((uint16_t)app->cfg->control_port);
    a.sin_addr.s_addr = htonl(INADDR_ANY);
    if (bind(listen_fd, (struct sockaddr *)&a, sizeof a) != 0 || listen(listen_fd, 16) != 0) {
        LOGE("control", "cannot listen on port %d: %s", app->cfg->control_port, strerror(errno));
        close(listen_fd);
        listen_fd = -1;
        return -1;
    }
    LOGI("control", "live settings API on port %d", app->cfg->control_port);
    return pthread_create(&thr, NULL, server_thread, NULL);
}

void control_stop(void)
{
    if (listen_fd < 0) return;
    pthread_join(thr, NULL);
    close(listen_fd);
    listen_fd = -1;
}
