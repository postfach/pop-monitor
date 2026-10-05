#ifndef POP_UPLOADER_H
#define POP_UPLOADER_H

#include "app.h"

int  uploader_start(app_ctx *app);   /* outbox sender + heartbeat */
void uploader_join(void);

#endif
