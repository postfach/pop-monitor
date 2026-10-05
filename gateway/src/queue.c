#define _GNU_SOURCE
#include "queue.h"
#include "util.h"

#include <dirent.h>
#include <openssl/evp.h>
#include <openssl/hmac.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

static pthread_mutex_t mu = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t cv = PTHREAD_COND_INITIALIZER;
static int pending_wake = 0;
static unsigned seq = 0;

int queue_init(const char *dir)
{
    char d[1100];
    snprintf(d, sizeof d, "%s/dead", dir);
    if (util_mkdir_p(dir) != 0 || util_mkdir_p(d) != 0) return -1;
    return 0;
}

void queue_path(const char *dir, const char *stem, const char *suffix, char *out, size_t n)
{
    snprintf(out, n, "%s/%s%s", dir, stem, suffix);
}

int queue_put(const char *dir, int prio, const char *json, const uint8_t *frame, size_t fl,
              const uint8_t *roi, size_t rl, char *stem_out, size_t sn)
{
    char stem[96], uid[37], p[1200];
    util_uuid4(uid);
    pthread_mutex_lock(&mu);
    unsigned s = ++seq;
    pthread_mutex_unlock(&mu);
    snprintf(stem, sizeof stem, "%d-%013lld-%06u-%.8s", prio, (long long)util_wall_ms(), s % 1000000, uid);
    if (frame && fl) {
        queue_path(dir, stem, ".frame.jpg", p, sizeof p);
        if (util_write_file_atomic(p, frame, fl) != 0) return -1;
    }
    if (roi && rl) {
        queue_path(dir, stem, ".roi.jpg", p, sizeof p);
        if (util_write_file_atomic(p, roi, rl) != 0) return -1;
    }
    queue_path(dir, stem, ".json", p, sizeof p);
    if (util_write_file_atomic(p, json, strlen(json)) != 0) return -1;
    if (stem_out) snprintf(stem_out, sn, "%s", stem);
    queue_wake();
    return 0;
}

static int is_json(const char *name, char *stem, size_t n)
{
    size_t L = strlen(name);
    if (L < 6 || strcmp(name + L - 5, ".json") != 0) return 0;
    snprintf(stem, n, "%.*s", (int)(L - 5), name);
    return 1;
}

int queue_next(const char *dir, char *best, size_t n)
{
    DIR *d = opendir(dir);
    if (!d) return 0;
    struct dirent *e;
    int found = 0;
    char stem[256];
    while ((e = readdir(d))) {
        if (!is_json(e->d_name, stem, sizeof stem)) continue;
        if (!found || strcmp(stem, best) < 0) {
            snprintf(best, n, "%s", stem);
            found = 1;
        }
    }
    closedir(d);
    return found;
}

void queue_remove(const char *dir, const char *stem)
{
    static const char *suf[] = {".json", ".frame.jpg", ".roi.jpg"};
    char p[1200];
    for (int i = 0; i < 3; i++) {
        queue_path(dir, stem, suf[i], p, sizeof p);
        unlink(p);
    }
}

void queue_dead_letter(const char *dir, const char *stem)
{
    static const char *suf[] = {".frame.jpg", ".roi.jpg", ".json"};
    char a[1200], b[1200];
    for (int i = 0; i < 3; i++) {
        snprintf(a, sizeof a, "%s/%s%s", dir, stem, suf[i]);
        snprintf(b, sizeof b, "%s/dead/%s%s", dir, stem, suf[i]);
        rename(a, b);
    }
}

typedef struct { char name[256]; off_t size; } entry;

static int cmp_entry(const void *a, const void *b)
{
    return strcmp(((const entry *)a)->name, ((const entry *)b)->name);
}

static entry *list_files(const char *dir, int *count, uint64_t *bytes)
{
    DIR *d = opendir(dir);
    *count = 0;
    *bytes = 0;
    if (!d) return NULL;
    int cap = 256, n = 0;
    entry *v = malloc(sizeof(entry) * cap);
    struct dirent *e;
    char p[1200];
    while ((e = readdir(d))) {
        if (e->d_name[0] == '.' || !strcmp(e->d_name, "dead")) continue;
        snprintf(p, sizeof p, "%s/%s", dir, e->d_name);
        struct stat st;
        if (stat(p, &st) != 0 || !S_ISREG(st.st_mode)) continue;
        if (n == cap) { cap *= 2; v = realloc(v, sizeof(entry) * cap); }
        snprintf(v[n].name, sizeof v[n].name, "%s", e->d_name);
        v[n].size = st.st_size;
        *bytes += (uint64_t)st.st_size;
        n++;
    }
    closedir(d);
    qsort(v, (size_t)n, sizeof(entry), cmp_entry);
    *count = n;
    return v;
}

void queue_stats(const char *dir, int *count, uint64_t *bytes)
{
    int n;
    entry *v = list_files(dir, &n, bytes);
    int recs = 0;
    for (int i = 0; i < n; i++) {
        size_t L = strlen(v[i].name);
        if (L > 5 && !strcmp(v[i].name + L - 5, ".json")) recs++;
    }
    free(v);
    *count = recs;
}

void queue_enforce_cap(const char *dir, uint64_t max_bytes)
{
    int n;
    uint64_t total;
    entry *v = list_files(dir, &n, &total);
    if (!v || total <= max_bytes) { free(v); return; }
    char p[1200];
    int dropped = 0;
    /* pass 1: full frames of the oldest PoP records */
    for (int i = 0; i < n && total > max_bytes; i++) {
        if (!strstr(v[i].name, ".frame.jpg")) continue;
        snprintf(p, sizeof p, "%s/%s", dir, v[i].name);
        if (unlink(p) == 0) { total -= (uint64_t)v[i].size; dropped++; }
    }
    /* pass 2: whole records, oldest first */
    for (int i = 0; i < n && total > max_bytes; i++) {
        snprintf(p, sizeof p, "%s/%s", dir, v[i].name);
        if (access(p, F_OK) != 0) continue;
        char stem[256];
        snprintf(stem, sizeof stem, "%s", v[i].name);
        char *dot = strchr(stem, '.');
        if (dot) *dot = 0;
        /* remove the record's siblings together so no orphans remain */
        for (int j = i; j < n && !strncmp(v[j].name, stem, strlen(stem)); j++) {
            snprintf(p, sizeof p, "%s/%s", dir, v[j].name);
            if (access(p, F_OK) == 0) total -= (uint64_t)v[j].size;
        }
        queue_remove(dir, stem);
        dropped++;
    }
    if (dropped) LOGW("queue", "queue over %llu MB: dropped %d oldest files",
                      (unsigned long long)(max_bytes >> 20), dropped);
    free(v);
}

void queue_wake(void)
{
    pthread_mutex_lock(&mu);
    pending_wake = 1;
    pthread_cond_signal(&cv);
    pthread_mutex_unlock(&mu);
}

void queue_wait(int timeout_ms)
{
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    ts.tv_sec += timeout_ms / 1000;
    ts.tv_nsec += (long)(timeout_ms % 1000) * 1000000L;
    if (ts.tv_nsec >= 1000000000L) { ts.tv_sec++; ts.tv_nsec -= 1000000000L; }
    pthread_mutex_lock(&mu);
    if (!pending_wake) pthread_cond_timedwait(&cv, &mu, &ts);
    pending_wake = 0;
    pthread_mutex_unlock(&mu);
}

static void hex(const unsigned char *d, unsigned n, char *out)
{
    static const char *h = "0123456789abcdef";
    for (unsigned i = 0; i < n; i++) { out[2 * i] = h[d[i] >> 4]; out[2 * i + 1] = h[d[i] & 15]; }
    out[2 * n] = 0;
}

void sha256_hex(const void *data, size_t n, char out[65])
{
    unsigned char md[EVP_MAX_MD_SIZE];
    unsigned len = 0;
    EVP_Digest(data, n, md, &len, EVP_sha256(), NULL);
    hex(md, len, out);
}

void hmac_sha256_hex(const char *key, const void *data, size_t n, char out[65])
{
    unsigned char md[EVP_MAX_MD_SIZE];
    unsigned len = 0;
    HMAC(EVP_sha256(), key, (int)strlen(key), data, n, md, &len);
    hex(md, len, out);
}
