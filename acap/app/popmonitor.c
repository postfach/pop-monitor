/* PoP Monitor ACAP (phase 2 scaffold): the same popcore detection running
 * on the camera, fed by VDO instead of RTSP.
 *
 * Status: compiles against ACAP Native SDK 12 APIs as documented; not yet
 * built or run on a camera. Uploading reuses the gateway's record format;
 * until libcurl is bundled (see Axis "curl-openssl" example) records are
 * written to the app's localdata queue for pickup.
 */
#include "popcore.h"

#include <axsdk/axparameter.h>
#include <glib.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <syslog.h>
#include <time.h>
#include <vdo-buffer.h>
#include <vdo-frame.h>
#include <vdo-map.h>
#include <vdo-stream.h>
#include <vdo-types.h>

#define APP "popmonitor"
#define QUEUE_DIR "/usr/local/packages/popmonitor/localdata/queue"
#define AW 640
#define AH 360

static volatile sig_atomic_t running = 1;
static void on_sig(int s) { (void)s; running = 0; }

typedef struct {
    char       site_id[64];
    pc_point   roi[4];
    float      inset;
    int        fps;
    pc_profile profile;
    int        snapshot_delay_ms;
} settings;

static int64_t mono_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static int param_int(AXParameter *h, const char *name, int def)
{
    gchar *v = NULL;
    GError *e = NULL;
    int out = def;
    if (ax_parameter_get(h, name, &v, &e) && v) out = atoi(v);
    g_free(v);
    if (e) g_error_free(e);
    return out;
}

static void param_str(AXParameter *h, const char *name, char *dst, size_t n)
{
    gchar *v = NULL;
    GError *e = NULL;
    if (ax_parameter_get(h, name, &v, &e) && v) snprintf(dst, n, "%s", v);
    g_free(v);
    if (e) g_error_free(e);
}

static void load_settings(AXParameter *h, settings *s)
{
    char roi[128] = "0,0 1,0 1,1 0,1";
    param_str(h, "SiteId", s->site_id, sizeof s->site_id);
    param_str(h, "RoiCorners", roi, sizeof roi);
    if (sscanf(roi, "%f,%f %f,%f %f,%f %f,%f", &s->roi[0].x, &s->roi[0].y, &s->roi[1].x, &s->roi[1].y,
               &s->roi[2].x, &s->roi[2].y, &s->roi[3].x, &s->roi[3].y) != 8)
        syslog(LOG_WARNING, "RoiCorners '%s' is not 4 x,y pairs", roi);
    s->inset = (float)param_int(h, "RoiInsetPct", 3);
    s->fps = param_int(h, "AnalysisFps", 5);
    s->profile.slot_count = param_int(h, "SlotCount", 0);
    s->profile.slot_ms = param_int(h, "SlotLengthMs", 0);
    s->snapshot_delay_ms = param_int(h, "SnapshotDelayMs", 1000);
}

static VdoStream *open_stream(int fps, GError **err)
{
    VdoMap *m = vdo_map_new();
    vdo_map_set_uint32(m, "format", VDO_FORMAT_YUV);
    vdo_map_set_string(m, "subformat", "NV12");
    vdo_map_set_uint32(m, "width", AW);
    vdo_map_set_uint32(m, "height", AH);
    vdo_map_set_double(m, "framerate", fps);
    VdoStream *s = vdo_stream_new(m, NULL, err);
    g_object_unref(m);
    if (s && !vdo_stream_start(s, err)) {
        g_object_unref(s);
        return NULL;
    }
    return s;
}

/* Full-resolution JPEG snapshot for the PoP record. */
static void save_snapshot(const settings *st, const pc_event *ev)
{
    GError *err = NULL;
    VdoMap *m = vdo_map_new();
    vdo_map_set_uint32(m, "format", VDO_FORMAT_JPEG);
    VdoBuffer *b = vdo_stream_snapshot(m, &err);
    g_object_unref(m);
    if (!b) {
        syslog(LOG_ERR, "snapshot failed: %s", err ? err->message : "?");
        if (err) g_error_free(err);
        return;
    }
    VdoFrame *f = vdo_buffer_get_frame(b);
    gsize size = vdo_frame_get_size(f);
    gpointer data = vdo_buffer_get_data(b);
    char path[256];
    snprintf(path, sizeof path, QUEUE_DIR "/1-%llu-c%04d.frame.jpg",
             (unsigned long long)ev->sequence, ev->creative_id);
    g_mkdir_with_parents(QUEUE_DIR, 0750);
    g_file_set_contents(path, data, (gssize)size, NULL);
    /* TODO(phase 2): burst + pc_snapshot_score, ROI crop, JSON metadata in
     * the gateway's format, HTTPS upload with libcurl. */
    syslog(LOG_INFO, "PoP %s #%llu c-%04d saved (%zu bytes)", st->site_id,
           (unsigned long long)ev->sequence, ev->creative_id, (size_t)size);
    g_object_unref(b);
}

int main(void)
{
    openlog(APP, LOG_PID, LOG_USER);
    signal(SIGTERM, on_sig);
    signal(SIGINT, on_sig);

    GError *err = NULL;
    AXParameter *ph = ax_parameter_new(APP, &err);
    if (!ph) {
        syslog(LOG_ERR, "axparameter: %s", err ? err->message : "?");
        return 1;
    }
    settings st;
    memset(&st, 0, sizeof st);
    load_settings(ph, &st);

    pc_detector det;
    pc_faults fm;
    pc_detector_init(&det, NULL, &st.profile);
    pc_faults_init(&fm, NULL, NULL, &st.profile);
    pc_warp warp;
    int ow = 192, oh = 96;
    if (pc_warp_init(&warp, AW, AH, st.roi, st.inset, ow, oh) != 0) {
        syslog(LOG_ERR, "invalid ROI");
        return 1;
    }
    static uint8_t y[192 * 96], u[192 * 96], v[192 * 96];

    VdoStream *vs = open_stream(st.fps, &err);
    if (!vs) {
        syslog(LOG_ERR, "vdo: %s", err ? err->message : "?");
        return 1;
    }
    syslog(LOG_INFO, "running: site %s, %dx%d at %d fps, profile %dx%d ms", st.site_id, AW, AH,
           st.fps, st.profile.slot_count, st.profile.slot_ms);

    int64_t last_reload = mono_ms();
    while (running) {
        VdoBuffer *buf = vdo_stream_get_buffer(vs, &err);
        if (!buf) {
            syslog(LOG_WARNING, "vdo buffer: %s", err ? err->message : "?");
            g_clear_error(&err);
            continue;
        }
        uint8_t *data = vdo_buffer_get_data(buf);
        pc_frame f = {PC_FMT_NV12, AW, AH, {data, data + AW * AH, NULL}, {AW, AW, 0}};
        pc_warp_apply(&warp, &f, y, u, v);
        vdo_stream_buffer_unref(vs, &buf, NULL);

        int64_t now = mono_ms();
        pc_features fe;
        pc_features_compute(y, u, v, ow, oh, &fe);
        pc_event ev;
        int fired = pc_detector_push(&det, &fe, now, &ev);
        if (fired) save_snapshot(&st, &ev);   /* TODO: move off this thread, honour delay */
        pc_faults_push(&fm, &fe, fired ? &ev : NULL, now);
        pc_fault_event fev;
        while (pc_faults_pop(&fm, &fev))
            syslog(fev.raised ? LOG_WARNING : LOG_INFO, "fault %s %s: %s", pc_fault_name(fev.type),
                   fev.one_shot ? "occurred" : (fev.raised ? "raised" : "cleared"), fev.detail);
        /* TODO(phase 2): declare faults as axevent events; post alerts. */

        if (now - last_reload > 30000) {     /* pick up settings changes */
            load_settings(ph, &st);
            pc_detector_set_profile(&det, &st.profile);
            pc_faults_set_profile(&fm, &st.profile);
            pc_warp_free(&warp);
            pc_warp_init(&warp, AW, AH, st.roi, st.inset, ow, oh);
            last_reload = now;
        }
    }
    pc_warp_free(&warp);
    g_object_unref(vs);
    ax_parameter_free(ph);
    closelog();
    return 0;
}
