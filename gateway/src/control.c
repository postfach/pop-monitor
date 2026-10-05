#define _GNU_SOURCE
#include "control.h"
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

#define MAX_BODY 65536

static app_ctx *A;
static pthread_t thr;
static int listen_fd = -1;
static pthread_mutex_t save_mu = PTHREAD_MUTEX_INITIALIZER;

/* ---------------- region validation, persistence ---------------- */

static double cross(pc_point o, pc_point a, pc_point b)
{
    return (double)(a.x - o.x) * (b.y - o.y) - (double)(a.y - o.y) * (b.x - o.x);
}

/* Four corners TL,TR,BR,BL inside 0..1, forming a convex quad of real size. */
static int roi_valid(const pc_point r[4], char *why, size_t n)
{
    for (int i = 0; i < 4; i++)
        if (!(r[i].x >= 0 && r[i].x <= 1 && r[i].y >= 0 && r[i].y <= 1)) {
            snprintf(why, n, "corners must be between 0 and 1");
            return 0;
        }
    double s0 = cross(r[0], r[1], r[2]), s1 = cross(r[1], r[2], r[3]);
    double s2 = cross(r[2], r[3], r[0]), s3 = cross(r[3], r[0], r[1]);
    int pos = (s0 > 0) + (s1 > 0) + (s2 > 0) + (s3 > 0);
    if (pos != 4 && pos != 0) {
        snprintf(why, n, "corners must form a box in the order top-left, top-right, bottom-right, bottom-left");
        return 0;
    }
    double area = fabs(s0 + s2) / 2;   /* two triangles of the quad */
    if (area < 0.0005) {
        snprintf(why, n, "region is too small");
        return 0;
    }
    return 1;
}

static void save_overrides(void)
{
    const gateway_cfg *g = A->cfg;
    cJSON *root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "note", "Regions edited from the review page. These override config.json.");
    cJSON *cams = cJSON_AddObjectToObject(root, "cameras");
    for (int i = 0; i < g->n_cameras; i++) {
        pc_point r[4];
        float inset;
        status_get_roi(&A->status[i], r, &inset);
        cJSON *c = cJSON_AddObjectToObject(cams, g->cameras[i].name);
        cJSON *arr = cJSON_AddArrayToObject(c, "roi");
        for (int k = 0; k < 4; k++) {
            cJSON *pt = cJSON_CreateArray();
            cJSON_AddItemToArray(pt, cJSON_CreateNumber(round((double)r[k].x * 10000) / 10000));
            cJSON_AddItemToArray(pt, cJSON_CreateNumber(round((double)r[k].y * 10000) / 10000));
            cJSON_AddItemToArray(arr, pt);
        }
        cJSON_AddNumberToObject(c, "inset_pct", inset);
    }
    char *txt = cJSON_Print(root);
    cJSON_Delete(root);
    pthread_mutex_lock(&save_mu);
    if (util_write_file_atomic(g->state_file, txt, strlen(txt)) != 0)
        LOGE("control", "cannot save region edits to %s", g->state_file);
    pthread_mutex_unlock(&save_mu);
    free(txt);
}

static int parse_roi(const cJSON *arr, pc_point r[4])
{
    if (!cJSON_IsArray(arr) || cJSON_GetArraySize(arr) != 4) return -1;
    for (int i = 0; i < 4; i++) {
        const cJSON *pt = cJSON_GetArrayItem(arr, i);
        if (!cJSON_IsArray(pt) || cJSON_GetArraySize(pt) != 2) return -1;
        const cJSON *x = cJSON_GetArrayItem(pt, 0), *y = cJSON_GetArrayItem(pt, 1);
        if (!cJSON_IsNumber(x) || !cJSON_IsNumber(y)) return -1;
        r[i].x = (float)x->valuedouble;
        r[i].y = (float)y->valuedouble;
    }
    return 0;
}

void control_init_regions(app_ctx *app)
{
    A = app;
    const gateway_cfg *g = app->cfg;
    for (int i = 0; i < g->n_cameras; i++) {
        memcpy(app->status[i].roi, g->cameras[i].roi, sizeof app->status[i].roi);
        app->status[i].inset_pct = g->cameras[i].inset_pct;
    }
    size_t len;
    char *txt = util_read_file(g->state_file, &len);
    if (!txt) return;
    cJSON *root = cJSON_Parse(txt);
    free(txt);
    const cJSON *cams = root ? cJSON_GetObjectItemCaseSensitive(root, "cameras") : NULL;
    int applied = 0;
    for (int i = 0; cams && i < g->n_cameras; i++) {
        const cJSON *c = cJSON_GetObjectItemCaseSensitive(cams, g->cameras[i].name);
        pc_point r[4];
        char why[160];
        if (!c || parse_roi(cJSON_GetObjectItemCaseSensitive(c, "roi"), r) != 0 || !roi_valid(r, why, sizeof why))
            continue;
        memcpy(app->status[i].roi, r, sizeof r);
        const cJSON *in = cJSON_GetObjectItemCaseSensitive(c, "inset_pct");
        if (cJSON_IsNumber(in)) app->status[i].inset_pct = (float)in->valuedouble;
        applied++;
    }
    cJSON_Delete(root);
    if (applied) LOGI("control", "using regions edited on the review page for %d camera(s) (%s)", applied, g->state_file);
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
    const char *reason = code == 200 ? "OK" : code == 400 ? "Bad Request" : code == 401 ? "Unauthorized"
                       : code == 404 ? "Not Found" : code == 502 ? "Bad Gateway" : "Error";
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

static int find_camera(const char *name)
{
    for (int i = 0; i < A->cfg->n_cameras; i++)
        if (!strcmp(A->cfg->cameras[i].name, name)) return i;
    return -1;
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

static void handle_list(int fd)
{
    cJSON *arr = cJSON_CreateArray();
    for (int i = 0; i < A->cfg->n_cameras; i++) {
        const camera_cfg *c = &A->cfg->cameras[i];
        cam_status *s = &A->status[i];
        cJSON *o = cJSON_CreateObject();
        cJSON_AddStringToObject(o, "name", c->name);
        cJSON_AddStringToObject(o, "site_id", c->site_id);
        cJSON_AddStringToObject(o, "model", c->model);
        pthread_mutex_lock(&s->mu);
        cJSON_AddBoolToObject(o, "connected", s->connected);
        cJSON *r = cJSON_AddArrayToObject(o, "roi");
        for (int k = 0; k < 4; k++) {
            cJSON *pt = cJSON_CreateArray();
            cJSON_AddItemToArray(pt, cJSON_CreateNumber(round((double)s->roi[k].x * 10000) / 10000));
            cJSON_AddItemToArray(pt, cJSON_CreateNumber(round((double)s->roi[k].y * 10000) / 10000));
            cJSON_AddItemToArray(r, pt);
        }
        cJSON_AddNumberToObject(o, "inset_pct", s->inset_pct);
        pthread_mutex_unlock(&s->mu);
        cJSON_AddItemToArray(arr, o);
    }
    respond_json(fd, 200, arr);
}

static void handle_snapshot(int fd, int idx)
{
    const camera_cfg *c = &A->cfg->cameras[idx];
    vapix vx;
    if (vapix_init(&vx, c->vapix_url, c->username, c->password,
                   c->camera_tls_insecure || A->cfg->tls_insecure) != 0) {
        respond_error(fd, 500, "curl init failed");
        return;
    }
    char q[96];
    snprintf(q, sizeof q, "compression=25%s%s", *c->snap.resolution ? "&resolution=" : "", c->snap.resolution);
    uint8_t *jpg;
    size_t len;
    if (vapix_jpeg(&vx, q, &jpg, &len) != 0) {
        vapix_free(&vx);
        respond_error(fd, 502, "could not get a snapshot from the camera");
        return;
    }
    vapix_free(&vx);
    respond(fd, 200, "image/jpeg", jpg, len);
    free(jpg);
}

static void handle_set_roi(int fd, int idx, const char *body)
{
    cJSON *j = cJSON_Parse(body);
    pc_point r[4];
    char why[160];
    if (!j || parse_roi(cJSON_GetObjectItemCaseSensitive(j, "roi"), r) != 0) {
        cJSON_Delete(j);
        respond_error(fd, 400, "expected {\"roi\": [[x,y],[x,y],[x,y],[x,y]], \"inset_pct\": n}");
        return;
    }
    if (!roi_valid(r, why, sizeof why)) {
        cJSON_Delete(j);
        respond_error(fd, 400, why);
        return;
    }
    cam_status *s = &A->status[idx];
    const cJSON *in = cJSON_GetObjectItemCaseSensitive(j, "inset_pct");
    pthread_mutex_lock(&s->mu);
    memcpy(s->roi, r, sizeof r);
    if (cJSON_IsNumber(in) && in->valuedouble >= 0 && in->valuedouble <= 20) s->inset_pct = (float)in->valuedouble;
    s->roi_version++;
    pthread_mutex_unlock(&s->mu);
    cJSON_Delete(j);
    save_overrides();
    LOGI(A->cfg->cameras[idx].name, "region changed from the review page");
    cJSON *ok = cJSON_CreateObject();
    cJSON_AddStringToObject(ok, "status", "applied");
    respond_json(fd, 200, ok);
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
    while (have < clen && have < MAX_BODY) {
        ssize_t r = recv(fd, body + have, MAX_BODY - have, 0);
        if (r <= 0) break;
        have += (size_t)r;
    }
    body[have < MAX_BODY ? have : MAX_BODY] = 0;

    /* auth: Bearer <api_key> */
    char want[300];
    snprintf(want, sizeof want, "Bearer %s", A->cfg->api_key);
    while (auth && *auth == ' ') auth++;
    size_t wl = strlen(want);
    if (!auth || strncmp(auth, want, wl) != 0 || (auth[wl] != '\r' && auth[wl] != 0)) {
        respond_error(fd, 401, "missing or wrong API key");
        free(buf);
        return;
    }

    char *q = strchr(path, '?');
    if (q) *q = 0;
    if (!strcmp(method, "GET") && !strcmp(path, "/api/cameras")) {
        handle_list(fd);
    } else if (!strncmp(path, "/api/cameras/", 13)) {
        char name[512];
        snprintf(name, sizeof name, "%s", path + 13);
        char *slash = strrchr(name, '/');
        const char *action = slash ? slash + 1 : "";
        if (slash) *slash = 0;
        url_decode(name);
        int idx = find_camera(name);
        if (idx < 0) respond_error(fd, 404, "no such camera");
        else if (!strcmp(method, "GET") && !strcmp(action, "snapshot")) handle_snapshot(fd, idx);
        else if ((!strcmp(method, "PUT") || !strcmp(method, "POST")) && !strcmp(action, "roi"))
            handle_set_roi(fd, idx, body);
        else respond_error(fd, 404, "unknown endpoint");
    } else if (!strcmp(path, "/healthz")) {
        respond(fd, 200, "text/plain", "ok", 2);
    } else {
        respond_error(fd, 404, "unknown endpoint");
    }
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
