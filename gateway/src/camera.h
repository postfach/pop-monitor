#ifndef POP_CAMERA_H
#define POP_CAMERA_H

#include "app.h"

/* Starts the detection and snapshot threads for camera `idx`. */
int  camera_start(app_ctx *app, int idx);
void camera_join_all(void);

#endif
