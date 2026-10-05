#include "vapix.h"
#include "util.h"

#include <curl/curl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef struct { uint8_t *p; size_t n, cap; } membuf;

static size_t on_data(void *ptr, size_t sz, size_t nm, void *ud)
{
    membuf *m = ud;
    size_t add = sz * nm;
    if (m->n + add + 1 > m->cap) {
        size_t cap = m->cap ? m->cap * 2 : 65536;
        while (cap < m->n + add + 1) cap *= 2;
        if (cap > 64u * 1024 * 1024) return 0;         /* refuse absurd responses */
        uint8_t *np = realloc(m->p, cap);
        if (!np) return 0;
        m->p = np;
        m->cap = cap;
    }
    memcpy(m->p + m->n, ptr, add);
    m->n += add;
    m->p[m->n] = 0;
    return add;
}

int vapix_init(vapix *v, const char *base, const char *user, const char *pass, int insecure)
{
    memset(v, 0, sizeof *v);
    snprintf(v->base, sizeof v->base, "%s", base);
    size_t L = strlen(v->base);
    if (L && v->base[L - 1] == '/') v->base[L - 1] = 0;
    snprintf(v->userpwd, sizeof v->userpwd, "%s:%s", user ? user : "", pass ? pass : "");
    v->insecure = insecure;
    v->curl = curl_easy_init();
    return v->curl ? 0 : -1;
}

void vapix_free(vapix *v)
{
    if (v->curl) curl_easy_cleanup(v->curl);
    v->curl = NULL;
}

static int get(vapix *v, const char *path, membuf *out, long timeout_ms, long *status)
{
    CURL *c = v->curl;
    char url[600];
    snprintf(url, sizeof url, "%s%s", v->base, path);
    curl_easy_reset(c);
    curl_easy_setopt(c, CURLOPT_URL, url);
    curl_easy_setopt(c, CURLOPT_HTTPAUTH, (long)(CURLAUTH_DIGEST | CURLAUTH_BASIC));
    curl_easy_setopt(c, CURLOPT_USERPWD, v->userpwd);
    curl_easy_setopt(c, CURLOPT_WRITEFUNCTION, on_data);
    curl_easy_setopt(c, CURLOPT_WRITEDATA, out);
    curl_easy_setopt(c, CURLOPT_TIMEOUT_MS, timeout_ms);
    curl_easy_setopt(c, CURLOPT_CONNECTTIMEOUT_MS, 5000L);
    curl_easy_setopt(c, CURLOPT_NOSIGNAL, 1L);
    if (v->insecure) {
        curl_easy_setopt(c, CURLOPT_SSL_VERIFYPEER, 0L);
        curl_easy_setopt(c, CURLOPT_SSL_VERIFYHOST, 0L);
    }
    CURLcode rc = curl_easy_perform(c);
    curl_easy_getinfo(c, CURLINFO_RESPONSE_CODE, status);
    if (rc != CURLE_OK) {
        LOGW("vapix", "%s: %s", url, curl_easy_strerror(rc));
        return -1;
    }
    return 0;
}

int vapix_jpeg(vapix *v, const char *query, uint8_t **buf, size_t *len)
{
    char path[400];
    snprintf(path, sizeof path, "/axis-cgi/jpg/image.cgi%s%s", query && *query ? "?" : "",
             query ? query : "");
    membuf m = {0};
    long status = 0;
    if (get(v, path, &m, 10000, &status) != 0 || status != 200 || m.n < 4 ||
        m.p[0] != 0xFF || m.p[1] != 0xD8) {
        if (status && status != 200) LOGW("vapix", "%s%s -> HTTP %ld", v->base, path, status);
        free(m.p);
        return -1;
    }
    *buf = m.p;
    *len = m.n;
    return 0;
}

int vapix_ptz_position(vapix *v, float *pan, float *tilt, float *zoom)
{
    membuf m = {0};
    long status = 0;
    if (get(v, "/axis-cgi/com/ptz.cgi?query=position", &m, 5000, &status) != 0 || status != 200 || !m.p) {
        free(m.p);
        return -1;
    }
    int found = 0;
    char *save = NULL;
    for (char *line = strtok_r((char *)m.p, "\r\n", &save); line; line = strtok_r(NULL, "\r\n", &save)) {
        if (sscanf(line, "pan=%f", pan) == 1) found |= 1;
        else if (sscanf(line, "tilt=%f", tilt) == 1) found |= 2;
        else if (sscanf(line, "zoom=%f", zoom) == 1) found |= 4;
    }
    free(m.p);
    return found == 7 ? 0 : -1;
}
