/* Minimal VAPIX client: JPEG snapshots and PTZ position, digest/basic auth. */
#ifndef POP_VAPIX_H
#define POP_VAPIX_H

#include <stddef.h>
#include <stdint.h>

typedef struct {
    void *curl;               /* CURL*, kept for HTTP keep-alive */
    char  base[256];          /* e.g. http://10.0.0.5             */
    char  userpwd[160];
    int   insecure;
} vapix;

int  vapix_init(vapix *v, const char *base_url, const char *user, const char *pass, int insecure);
void vapix_free(vapix *v);
/* GET /axis-cgi/jpg/image.cgi?<query>. *buf is malloc'd. Returns 0 on success. */
int  vapix_jpeg(vapix *v, const char *query, uint8_t **buf, size_t *len);
/* GET /axis-cgi/com/ptz.cgi?query=position. Returns 0 on success, -1 if not a PTZ camera or error. */
int  vapix_ptz_position(vapix *v, float *pan, float *tilt, float *zoom);

#endif
