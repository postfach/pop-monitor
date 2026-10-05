/* Fault alerts: queued to the LUMO backend, and optionally posted
 * straight to Microsoft Teams (Workflows webhook) and Slack. */
#ifndef POP_ALERTS_H
#define POP_ALERTS_H

#include "app.h"
#include <stddef.h>
#include <stdint.h>

int  alerts_start(app_ctx *app);
void alerts_stop(void);

/* state: "raised", "cleared" or "occurred" (one-shot). roi_jpg may be NULL. */
void alerts_send(app_ctx *app, const camera_cfg *cam, const char *fault, const char *state,
                 int64_t since_wall, int64_t at_wall, const char *detail,
                 const uint8_t *roi_jpg, size_t roi_len);

/* POST a JSON body; returns HTTP status or -1. Shared with the uploader. */
long http_post_json(const char *url, const char *json, const char *bearer, int insecure, int timeout_s);

#endif
