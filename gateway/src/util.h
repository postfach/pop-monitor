#ifndef POP_UTIL_H
#define POP_UTIL_H

#include <stddef.h>
#include <stdint.h>

int64_t util_wall_ms(void);      /* epoch milliseconds (CLOCK_REALTIME) */
int64_t util_mono_ms(void);      /* monotonic milliseconds              */
void    util_sleep_ms(int ms);
void    util_iso8601(int64_t epoch_ms, char *buf, size_t n);   /* UTC, ms */
void    util_local_iso(int64_t epoch_ms, char *buf, size_t n); /* local (TZ), ms, +hh:mm */
void    util_local_time(int64_t epoch_ms, char *buf, size_t n);/* "2026-10-05 19:41:10 NZDT" */
int     util_clock_synced(void); /* 1 if the kernel reports NTP sync    */
void    util_uuid4(char out[37]);
int     util_mkdir_p(const char *path);
int     util_write_file_atomic(const char *path, const void *data, size_t n);
char   *util_read_file(const char *path, size_t *len);          /* malloc'd, NUL-terminated */
/* "HH:MM-HH:MM" (may wrap midnight; "00:00-24:00" = always). Local time. */
int     util_in_time_range(const char *range, int64_t epoch_ms);

/* Standard base64. util_base64 returns the length written (out NUL-terminated);
 * util_unbase64 returns bytes decoded (at most max) or -1 on bad input. */
int     util_base64(const unsigned char *in, size_t n, char *out, size_t cap);
int     util_unbase64(const char *in, unsigned char *out, size_t max);

enum { LOG_DEBUG = 0, LOG_INFO, LOG_WARN, LOG_ERROR };
extern int util_log_level;
void util_log(int level, const char *tag, const char *fmt, ...)
    __attribute__((format(printf, 3, 4)));

#define LOGD(tag, ...) util_log(LOG_DEBUG, tag, __VA_ARGS__)
#define LOGI(tag, ...) util_log(LOG_INFO, tag, __VA_ARGS__)
#define LOGW(tag, ...) util_log(LOG_WARN, tag, __VA_ARGS__)
#define LOGE(tag, ...) util_log(LOG_ERROR, tag, __VA_ARGS__)

#endif
