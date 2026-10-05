#ifndef POP_UPLOADER_H
#define POP_UPLOADER_H

#include "app.h"
#include <cjson/cJSON.h>

int  uploader_start(app_ctx *app);   /* outbox sender + heartbeat */
void uploader_join(void);
/* Gateway health (uploads, queue, disk, uptime) as JSON; caller frees. */
cJSON *gateway_health_json(void);

#endif
