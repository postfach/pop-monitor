/* Durable on-disk outbox. Each record = <stem>.json (+ <stem>.frame.jpg,
 * <stem>.roi.jpg). The .json is written last, so its presence marks a
 * complete record. Stems sort oldest first within a priority. */
#ifndef POP_QUEUE_H
#define POP_QUEUE_H

#include <stddef.h>
#include <stdint.h>

enum { QPRIO_ALERT = 0, QPRIO_POP = 1 };

int  queue_init(const char *dir);
int  queue_put(const char *dir, int prio, const char *json,
               const uint8_t *frame, size_t frame_len,
               const uint8_t *roi, size_t roi_len, char *stem_out, size_t stem_n);
/* Oldest record stem (highest priority first). 1 = found, 0 = empty. */
int  queue_next(const char *dir, char *stem, size_t n);
void queue_path(const char *dir, const char *stem, const char *suffix, char *out, size_t n);
void queue_remove(const char *dir, const char *stem);
void queue_dead_letter(const char *dir, const char *stem);
/* Keep total size under max_bytes: drop full frames of the oldest records
 * first (keeping metadata and ROI), then whole oldest records. */
void queue_enforce_cap(const char *dir, uint64_t max_bytes);
void queue_stats(const char *dir, int *count, uint64_t *bytes);
void queue_wake(void);               /* signal the uploader            */
void queue_wait(int timeout_ms);     /* used by the uploader           */

void sha256_hex(const void *data, size_t n, char out[65]);
void hmac_sha256_hex(const char *key, const void *data, size_t n, char out[65]);

#endif
