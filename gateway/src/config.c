#include "config.h"
#include "util.h"

#include <cjson/cJSON.h>
#include <dirent.h>
#include <sys/stat.h>
#include <unistd.h>
#include <math.h>
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
    pc_region_from_corners(&c->region, full);
    c->inset_pct = 3.f;
    c->analysis_fps = 5;
    snprintf(c->operating_hours, sizeof c->operating_hours, "00:00-24:00");
    c->ptz_check = 1;
    c->coverage_min_pct = 90;
    c->coverage_window_min = 30;
    snprintf(c->rtsp_transport, sizeof c->rtsp_transport, "tcp");
    snprintf(c->stream_params, sizeof c->stream_params, "resolution=1280x720&fps=10&videocodec=h264");
    c->ptz_tolerance = 1.0f;
    pc_detector_cfg_default(&c->det);
    pc_faults_cfg_default(&c->faults);
    c->snap.delay_ms = 1000;
    c->snap.burst = 3;
    c->snap.interval_ms = 300;
    c->snap.photo_every_min = 10;        /* every play is still logged; photos (camera snapshots,
                                          * disk, upload) for each creative every 10 minutes */
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
    get_int(o, "board_width", &c->board_width);
    get_int(o, "board_height", &c->board_height);
    get_int(o, "analysis_fps", &c->analysis_fps);
    get_int(o, "ptz_check", &c->ptz_check);
    get_int(o, "coverage_min_pct", &c->coverage_min_pct);
    get_int(o, "coverage_window_min", &c->coverage_window_min);
    get_str(o, "vault_screen", c->vault_screen, sizeof c->vault_screen);
    get_int(o, "expected_creatives", &c->expected_creatives);
    get_int(o, "setup", &c->setup);
    get_float(o, "ptz_tolerance", &c->ptz_tolerance);
    get_int(o, "realtime", &c->realtime);
    get_int(o, "loop", &c->loop);

    config_parse_region(o, &c->region);

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
        get_str(s, "image_policy", pol, sizeof pol);      /* the older setting */
        if (!strcmp(pol, "first_per_hour")) c->snap.photo_every_min = 60;
        else if (!strcmp(pol, "every_play")) c->snap.photo_every_min = 0;
        get_int(s, "photo_every_min", &c->snap.photo_every_min);
    }
}

static int validate_camera(camera_cfg *c, int idx, char *err, size_t n)
{
    if (c->coverage_min_pct < 0 || c->coverage_min_pct > 100) {
        snprintf(err, n, "camera %s: coverage alert threshold must be 0-100%% (0 = off)", c->site_id);
        return -1;
    }
    if (c->coverage_window_min < 5 || c->coverage_window_min > 240) {
        snprintf(err, n, "camera %s: coverage window must be 5-240 minutes", c->site_id);
        return -1;
    }
    if (!*c->site_id) { snprintf(err, n, "camera %d: site_id is required", idx); return -1; }
    if (c->http_port < 0 || c->http_port > 65535 || c->rtsp_port < 0 || c->rtsp_port > 65535) {
        snprintf(err, n, "camera %s: ports must be between 1 and 65535", c->site_id);
        return -1;
    }
    if (c->board_width < 0 || c->board_width > 20000 || c->board_height < 0 || c->board_height > 20000 ||
        (c->board_width > 0) != (c->board_height > 0)) {
        snprintf(err, n, "camera %s: screen resolution needs both width and height in pixels, or neither", c->site_id);
        return -1;
    }
    if (c->profile.slot_count < 0 || c->profile.slot_count > 200 || c->profile.slot_ms < 0 ||
        c->profile.slot_ms > 3600000) {
        snprintf(err, n, "camera %s: slot count or slot length is out of range", c->site_id);
        return -1;
    }
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
    char why[160];
    if (!config_region_valid(&c->region, why, sizeof why)) {
        snprintf(err, n, "camera %s: region: %s", c->name, why);
        return -1;
    }
    return 0;
}

/* ---------------- regions ---------------- */

static int parse_points(const cJSON *arr, pc_point *p, int n)
{
    if (!cJSON_IsArray(arr) || cJSON_GetArraySize(arr) != n) return -1;
    for (int i = 0; i < n; i++) {
        const cJSON *pt = cJSON_GetArrayItem(arr, i);
        if (!cJSON_IsArray(pt) || cJSON_GetArraySize(pt) != 2) return -1;
        const cJSON *x = cJSON_GetArrayItem(pt, 0), *y = cJSON_GetArrayItem(pt, 1);
        if (!cJSON_IsNumber(x) || !cJSON_IsNumber(y)) return -1;
        p[i].x = (float)x->valuedouble;
        p[i].y = (float)y->valuedouble;
    }
    return 0;
}

static cJSON *points_json(const pc_point *p, int n)
{
    cJSON *arr = cJSON_CreateArray();
    for (int k = 0; k < n; k++) {
        cJSON *pt = cJSON_CreateArray();
        cJSON_AddItemToArray(pt, cJSON_CreateNumber(round((double)p[k].x * 10000) / 10000));
        cJSON_AddItemToArray(pt, cJSON_CreateNumber(round((double)p[k].y * 10000) / 10000));
        cJSON_AddItemToArray(arr, pt);
    }
    return arr;
}

static void put(cJSON *obj, const char *key, cJSON *val)
{
    if (cJSON_GetObjectItemCaseSensitive(obj, key)) cJSON_ReplaceItemInObjectCaseSensitive(obj, key, val);
    else cJSON_AddItemToObject(obj, key, val);
}

int config_parse_region(const void *obj, pc_region *r)
{
    const cJSON *o = obj;
    pc_point c[4];
    if (parse_points(cJSON_GetObjectItemCaseSensitive(o, "roi"), c, 4) != 0) return -1;
    pc_region_from_corners(r, c);
    const cJSON *sh = cJSON_GetObjectItemCaseSensitive(o, "roi_shape");
    if (cJSON_IsString(sh)) {
        if (!strcmp(sh->valuestring, "curve")) r->shape = PC_SHAPE_CURVE;
        else if (!strcmp(sh->valuestring, "fold")) r->shape = PC_SHAPE_FOLD;
    }
    pc_point m[8], f[2];
    if (parse_points(cJSON_GetObjectItemCaseSensitive(o, "roi_curve"), m, 8) == 0) memcpy(r->curve, m, sizeof m);
    else if (parse_points(cJSON_GetObjectItemCaseSensitive(o, "roi_mid"), m, 4) == 0) pc_region_curve_from_mids(r, m);
    if (parse_points(cJSON_GetObjectItemCaseSensitive(o, "roi_fold"), f, 2) == 0) memcpy(r->fold, f, sizeof f);
    const cJSON *fp = cJSON_GetObjectItemCaseSensitive(o, "fold_pct");
    if (cJSON_IsNumber(fp)) r->fold_u = (float)(fp->valuedouble / 100.0);
    return 0;
}

void config_region_to_json(void *obj, const pc_region *r)
{
    cJSON *o = obj;
    put(o, "roi", points_json(r->c, 4));
    static const char *names[] = {"standard", "curve", "fold"};
    int s = r->shape >= 0 && r->shape <= 2 ? r->shape : 0;
    if (s == PC_SHAPE_STANDARD) {
        cJSON_DeleteItemFromObjectCaseSensitive(o, "roi_shape");
    } else {
        put(o, "roi_shape", cJSON_CreateString(names[s]));
    }
    cJSON_DeleteItemFromObjectCaseSensitive(o, "roi_mid");          /* the earlier one-handle curve */
    if (s == PC_SHAPE_CURVE) put(o, "roi_curve", points_json(r->curve, 8));
    else cJSON_DeleteItemFromObjectCaseSensitive(o, "roi_curve");
    if (s == PC_SHAPE_FOLD) {
        put(o, "roi_fold", points_json(r->fold, 2));
        put(o, "fold_pct", cJSON_CreateNumber(round((double)r->fold_u * 1000) / 10));
    } else {
        cJSON_DeleteItemFromObjectCaseSensitive(o, "roi_fold");
        cJSON_DeleteItemFromObjectCaseSensitive(o, "fold_pct");
    }
}

static double cross(pc_point o, pc_point a, pc_point b)
{
    return (double)(a.x - o.x) * (b.y - o.y) - (double)(a.y - o.y) * (b.x - o.x);
}

static int convex(pc_point a, pc_point b, pc_point c, pc_point d, double *area)
{
    double s0 = cross(a, b, c), s1 = cross(b, c, d), s2 = cross(c, d, a), s3 = cross(d, a, b);
    int pos = (s0 > 0) + (s1 > 0) + (s2 > 0) + (s3 > 0);
    *area = fabs(s0 + s2) / 2;
    return pos == 4 || pos == 0;
}

static int in01(pc_point p) { return p.x >= 0 && p.x <= 1 && p.y >= 0 && p.y <= 1; }

int config_region_valid(const pc_region *r, char *why, size_t n)
{
    for (int i = 0; i < 4; i++)
        if (!in01(r->c[i])) {
            snprintf(why, n, "corners must be between 0 and 1");
            return 0;
        }
    double area;
    if (!convex(r->c[0], r->c[1], r->c[2], r->c[3], &area)) {
        snprintf(why, n, "corners must form a box in the order top-left, top-right, bottom-right, bottom-left");
        return 0;
    }
    if (area < 0.0005) {
        snprintf(why, n, "region is too small");
        return 0;
    }
    if (r->shape == PC_SHAPE_CURVE) {
        for (int i = 0; i < 8; i++)
            if (!in01(r->curve[i])) {
                snprintf(why, n, "curve points must be inside the picture");
                return 0;
            }
    } else if (r->shape == PC_SHAPE_FOLD) {
        if (!in01(r->fold[0]) || !in01(r->fold[1])) {
            snprintf(why, n, "the fold line must be inside the picture");
            return 0;
        }
        if (!(r->fold_u >= 0.05f && r->fold_u <= 0.95f)) {
            snprintf(why, n, "the left face must be 5-95%% of the screen's width");
            return 0;
        }
        double a1, a2;
        if (!convex(r->c[0], r->fold[0], r->fold[1], r->c[3], &a1) ||
            !convex(r->fold[0], r->c[1], r->c[2], r->fold[1], &a2) || a1 < 0.0001 || a2 < 0.0001) {
            snprintf(why, n, "the fold line must run from the top edge to the bottom edge, between the corners");
            return 0;
        }
    }
    return 1;
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
    g->control_port = 8090;
    get_int(root, "control_port", &g->control_port);
    get_str(root, "state_file", g->state_file, sizeof g->state_file);
    if (!*g->state_file) {               /* default: next to the queue directory */
        char d[520];
        snprintf(d, sizeof d, "%s", g->queue_dir);
        size_t L = strlen(d);
        while (L > 1 && d[L - 1] == '/') d[--L] = 0;
        char *slash = strrchr(d, '/');
        if (slash && slash != d) *slash = 0; else snprintf(d, sizeof d, ".");
        snprintf(g->state_file, sizeof g->state_file, "%s/roi-overrides.json", d);
    }
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

    /* state directory: next to the queue directory */
    char d[520];
    snprintf(d, sizeof d, "%s", g->queue_dir);
    L = strlen(d);
    while (L > 1 && d[L - 1] == '/') d[--L] = 0;
    char *slash = strrchr(d, '/');
    if (slash && slash != d) *slash = 0; else snprintf(d, sizeof d, ".");
    snprintf(g->state_dir, sizeof g->state_dir, "%s", d);
    config_apply_staged_restore(g->state_dir);
    if (!*g->cameras_file) snprintf(g->cameras_file, sizeof g->cameras_file, "%s/cameras.json", d);

    const cJSON *defs = cJSON_GetObjectItemCaseSensitive(root, "defaults");
    g->defaults_json = defs ? cJSON_Duplicate(defs, 1) : cJSON_CreateObject();

    /* Camera list: cameras.json (managed from the review page) if it exists,
     * otherwise the "cameras" array in config.json. */
    cJSON *state = NULL;
    char *st = util_read_file(g->cameras_file, &len);
    if (st) {
        state = cJSON_Parse(st);
        free(st);
        if (!cJSON_IsArray(state)) {
            LOGE("config", "%s is not a JSON array; ignoring it and using config.json", g->cameras_file);
            cJSON_Delete(state);
            state = NULL;
        }
    }
    const cJSON *cams = state ? state : cJSON_GetObjectItemCaseSensitive(root, "cameras");
    g->cameras_from_state = state != NULL;
    int nc = cJSON_IsArray(cams) ? cJSON_GetArraySize(cams) : 0;
    if (nc > MAX_CAMERAS) { snprintf(err, n, "too many cameras (max %d)", MAX_CAMERAS); goto fail; }
    g->cameras = calloc(MAX_CAMERAS, sizeof(camera_cfg));
    for (int i = 0; i < nc; i++) {
        cJSON *obj = cJSON_Duplicate(cJSON_GetArrayItem(cams, i), 1);
        char e2[300];
        if (config_build_camera(g, obj, &g->cameras[i], e2, sizeof e2) != 0) {
            snprintf(err, n, "%s%s", e2, state ? " (in cameras.json, edited from the review page)" : "");
            cJSON_Delete(obj);
            goto fail;
        }
        g->cam_json[i] = obj;
        g->cameras[i].active = 1;
        g->n_cameras = i + 1;
        for (int k = 0; k < i; k++)
            if (!strcmp(g->cameras[k].site_id, g->cameras[i].site_id)) {
                snprintf(err, n, "site ID '%s' is used twice; site IDs must be unique", g->cameras[i].site_id);
                goto fail;
            }
    }
    cJSON_Delete(state);
    cJSON_Delete(root);
    return 0;
fail:
    cJSON_Delete(state);
    cJSON_Delete(root);
    config_free(g);
    return -1;
}

int config_build_camera(const gateway_cfg *g, const void *obj, camera_cfg *c, char *err, size_t n)
{
    camera_defaults(c);
    apply_camera(c, (const cJSON *)g->defaults_json);
    apply_camera(c, (const cJSON *)obj);
    return validate_camera(c, 0, err, n);
}

int config_find_camera(const gateway_cfg *g, const char *site_id)
{
    for (int i = 0; i < g->n_cameras; i++)
        if (g->cameras[i].active && !strcmp(g->cameras[i].site_id, site_id)) return i;
    return -1;
}

void config_library_path(const gateway_cfg *g, const char *site_id, char *out, size_t n)
{
    char safe[80];
    size_t k = 0;
    for (const char *p = site_id; *p && k + 1 < sizeof safe; p++)
        safe[k++] = ((*p >= 'a' && *p <= 'z') || (*p >= 'A' && *p <= 'Z') || (*p >= '0' && *p <= '9') ||
                     *p == '-' || *p == '_') ? *p : '_';
    safe[k] = 0;
    snprintf(out, n, "%s/creatives/%s.json", g->state_dir, safe);
}

int config_active_count(const gateway_cfg *g)
{
    int k = 0;
    for (int i = 0; i < g->n_cameras; i++) k += g->cameras[i].active;
    return k;
}

int config_save_cameras(const gateway_cfg *g)
{
    cJSON *arr = cJSON_CreateArray();
    for (int i = 0; i < g->n_cameras; i++)
        if (g->cameras[i].active && g->cam_json[i])
            cJSON_AddItemToArray(arr, cJSON_Duplicate((const cJSON *)g->cam_json[i], 1));
    char *txt = cJSON_Print(arr);
    cJSON_Delete(arr);
    int rc = util_write_file_atomic(g->cameras_file, txt, strlen(txt));
    if (rc != 0) LOGE("config", "cannot save cameras to %s", g->cameras_file);
    free(txt);
    return rc;
}

void config_free(gateway_cfg *g)
{
    for (int i = 0; i < MAX_CAMERAS; i++) { cJSON_Delete((cJSON *)g->cam_json[i]); g->cam_json[i] = NULL; }
    cJSON_Delete((cJSON *)g->defaults_json);
    g->defaults_json = NULL;
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

/* ---------------------------------------------------------------- restore */

static void rm_tree(const char *path)
{
    DIR *dir = opendir(path);
    if (!dir) { remove(path); return; }
    struct dirent *e;
    char p[1200];
    while ((e = readdir(dir))) {
        if (!strcmp(e->d_name, ".") || !strcmp(e->d_name, "..")) continue;
        snprintf(p, sizeof p, "%s/%s", path, e->d_name);
        rm_tree(p);
    }
    closedir(dir);
    rmdir(path);
}

int config_apply_staged_restore(const char *state_dir)
{
    char staged[700], ready[760], before[700], from[1200], to[1200];
    snprintf(staged, sizeof staged, "%s/restore-staged", state_dir);
    snprintf(ready, sizeof ready, "%s/READY", staged);
    struct stat sb;
    if (stat(ready, &sb) != 0) {
        if (stat(staged, &sb) == 0) rm_tree(staged);        /* half-written: ignore it */
        return 0;
    }
    snprintf(before, sizeof before, "%s/before-restore", state_dir);
    rm_tree(before);
    util_mkdir_p(before);
    /* what was there goes to before-restore/ (the sites, learned creatives, photo history) */
    static const char *parts[] = {"cameras.json", "creatives", "photos", NULL};
    for (int i = 0; parts[i]; i++) {
        snprintf(from, sizeof from, "%s/%s", state_dir, parts[i]);
        snprintf(to, sizeof to, "%s/%s", before, parts[i]);
        rename(from, to);
    }
    remove(ready);
    int n = 0;
    DIR *dir = opendir(staged);
    struct dirent *e;
    while (dir && (e = readdir(dir))) {
        if (e->d_name[0] == '.') continue;
        snprintf(from, sizeof from, "%s/%s", staged, e->d_name);
        snprintf(to, sizeof to, "%s/%s", state_dir, e->d_name);
        rm_tree(to);
        if (rename(from, to) == 0) n++;
    }
    if (dir) closedir(dir);
    rm_tree(staged);
    LOGI("config", "restored the gateway's state from a backup (%d item(s)); the previous state is in %s", n, before);
    return n;
}
