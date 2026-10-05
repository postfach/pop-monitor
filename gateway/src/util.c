#define _GNU_SOURCE
#include "util.h"

#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/timex.h>
#include <time.h>
#include <unistd.h>

int util_log_level = LOG_INFO;
static pthread_mutex_t log_mu = PTHREAD_MUTEX_INITIALIZER;

int64_t util_wall_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_REALTIME, &ts);
    return (int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

int64_t util_mono_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

void util_sleep_ms(int ms)
{
    if (ms <= 0) return;
    struct timespec ts = {ms / 1000, (long)(ms % 1000) * 1000000L};
    while (nanosleep(&ts, &ts) == -1 && errno == EINTR) {}
}

void util_iso8601(int64_t ms, char *buf, size_t n)
{
    time_t s = (time_t)(ms / 1000);
    struct tm tm;
    gmtime_r(&s, &tm);
    snprintf(buf, n, "%04d-%02d-%02dT%02d:%02d:%02d.%03dZ", tm.tm_year + 1900, tm.tm_mon + 1,
             tm.tm_mday, tm.tm_hour, tm.tm_min, tm.tm_sec, (int)(ms % 1000));
}

void util_local_iso(int64_t ms, char *buf, size_t n)
{
    time_t s = (time_t)(ms / 1000);
    struct tm tm;
    localtime_r(&s, &tm);
    long off = tm.tm_gmtoff / 60;
    char sign = off < 0 ? '-' : '+';
    if (off < 0) off = -off;
    snprintf(buf, n, "%04d-%02d-%02dT%02d:%02d:%02d.%03d%c%02ld:%02ld", tm.tm_year + 1900,
             tm.tm_mon + 1, tm.tm_mday, tm.tm_hour, tm.tm_min, tm.tm_sec, (int)(ms % 1000),
             sign, off / 60, off % 60);
}

void util_local_time(int64_t ms, char *buf, size_t n)
{
    time_t s = (time_t)(ms / 1000);
    struct tm tm;
    localtime_r(&s, &tm);
    if (!strftime(buf, n, "%Y-%m-%d %H:%M:%S %Z", &tm) && n) buf[0] = 0;
}

int util_clock_synced(void)
{
    struct timex tx;
    memset(&tx, 0, sizeof tx);
    int st = adjtimex(&tx);
    return st != TIME_ERROR && !(tx.status & STA_UNSYNC);
}

void util_uuid4(char out[37])
{
    unsigned char b[16];
    int fd = open("/dev/urandom", O_RDONLY);
    if (fd < 0 || read(fd, b, 16) != 16) {
        for (int i = 0; i < 16; i++) b[i] = (unsigned char)(rand() ^ (util_mono_ms() >> i));
    }
    if (fd >= 0) close(fd);
    b[6] = (b[6] & 0x0f) | 0x40;
    b[8] = (b[8] & 0x3f) | 0x80;
    snprintf(out, 37, "%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-%02x%02x%02x%02x%02x%02x",
             b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7], b[8], b[9], b[10], b[11], b[12],
             b[13], b[14], b[15]);
}

int util_mkdir_p(const char *path)
{
    char tmp[1024];
    snprintf(tmp, sizeof tmp, "%s", path);
    for (char *p = tmp + 1; *p; p++) {
        if (*p == '/') {
            *p = 0;
            if (mkdir(tmp, 0750) != 0 && errno != EEXIST) return -1;
            *p = '/';
        }
    }
    if (mkdir(tmp, 0750) != 0 && errno != EEXIST) return -1;
    return 0;
}

int util_write_file_atomic(const char *path, const void *data, size_t n)
{
    char tmp[1100];
    snprintf(tmp, sizeof tmp, "%s.tmp", path);
    FILE *f = fopen(tmp, "wb");
    if (!f) return -1;
    if (n && fwrite(data, 1, n, f) != n) { fclose(f); unlink(tmp); return -1; }
    fflush(f);
    fsync(fileno(f));
    fclose(f);
    if (rename(tmp, path) != 0) { unlink(tmp); return -1; }
    return 0;
}

char *util_read_file(const char *path, size_t *len)
{
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    fseek(f, 0, SEEK_END);
    long sz = ftell(f);
    fseek(f, 0, SEEK_SET);
    if (sz < 0) { fclose(f); return NULL; }
    char *buf = malloc((size_t)sz + 1);
    if (!buf) { fclose(f); return NULL; }
    size_t got = fread(buf, 1, (size_t)sz, f);
    fclose(f);
    buf[got] = 0;
    if (len) *len = got;
    return buf;
}

static int parse_hm(const char *s, int *mins)
{
    int h, m;
    if (sscanf(s, "%d:%d", &h, &m) != 2 || h < 0 || h > 24 || m < 0 || m > 59) return -1;
    *mins = h * 60 + m;
    return 0;
}

int util_in_time_range(const char *range, int64_t ms)
{
    if (!range || !*range) return 1;
    const char *dash = strchr(range, '-');
    int a, b;
    if (!dash || parse_hm(range, &a) || parse_hm(dash + 1, &b)) return 1;
    if (a == b || (a == 0 && b == 1440)) return 1;
    time_t s = (time_t)(ms / 1000);
    struct tm tm;
    localtime_r(&s, &tm);
    int now = tm.tm_hour * 60 + tm.tm_min;
    return a < b ? (now >= a && now < b) : (now >= a || now < b);
}

void util_log(int level, const char *tag, const char *fmt, ...)
{
    if (level < util_log_level) return;
    static const char *lv[] = {"DEBUG", "INFO", "WARN", "ERROR"};
    char ts[64];
    util_local_iso(util_wall_ms(), ts, sizeof ts);
    pthread_mutex_lock(&log_mu);
    fprintf(stderr, "%s %-5s [%s] ", ts, lv[level], tag ? tag : "-");
    va_list ap;
    va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
    fputc('\n', stderr);
    pthread_mutex_unlock(&log_mu);
}

static const char B64[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

int util_base64(const unsigned char *in, size_t n, char *out, size_t cap)
{
    size_t need = ((n + 2) / 3) * 4 + 1, o = 0;
    if (cap < need) { if (cap) out[0] = 0; return -1; }
    for (size_t i = 0; i < n; i += 3) {
        unsigned v = (unsigned)in[i] << 16;
        if (i + 1 < n) v |= (unsigned)in[i + 1] << 8;
        if (i + 2 < n) v |= in[i + 2];
        out[o++] = B64[(v >> 18) & 63];
        out[o++] = B64[(v >> 12) & 63];
        out[o++] = i + 1 < n ? B64[(v >> 6) & 63] : '=';
        out[o++] = i + 2 < n ? B64[v & 63] : '=';
    }
    out[o] = 0;
    return (int)o;
}

int util_unbase64(const char *in, unsigned char *out, size_t max)
{
    unsigned v = 0;
    int bits = 0;
    size_t o = 0;
    for (const char *p = in; *p && *p != '='; p++) {
        const char *k = strchr(B64, *p);
        if (!k || !*p) return -1;
        v = (v << 6) | (unsigned)(k - B64);
        bits += 6;
        if (bits >= 8) {
            bits -= 8;
            if (o >= max) return -1;
            out[o++] = (unsigned char)((v >> bits) & 0xff);
        }
    }
    return (int)o;
}
