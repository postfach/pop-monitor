/* Live-settings API used by the review page (through the receiver).
 *
 *   GET  /api/cameras                     cameras with their live region
 *   GET  /api/cameras/<name>/snapshot     a fresh JPEG from the camera
 *   PUT  /api/cameras/<name>/roi          {"roi":[[x,y]x4],"inset_pct":3,
 *                                          "roi_shape":"standard|curve|fold",
 *                                          "roi_curve":[[x,y]x8],"roi_fold":[[x,y]x2],"fold_pct":50}
 *
 * Every request needs "Authorization: Bearer <api_key>". Region edits apply
 * immediately and are saved to state_file, which overrides config.json. */
#ifndef POP_CONTROL_H
#define POP_CONTROL_H

#include "app.h"

void control_init_regions(app_ctx *app);   /* config + saved overrides -> status */
int  control_start(app_ctx *app);
void control_stop(void);

#endif
