/* popreplay: run the detection core over a recorded video, as fast as it
 * decodes, and print the plays and faults it finds. The tuning tool for the
 * field trial: record a loop from a real board, replay, adjust, repeat.
 *
 *   popreplay board.mp4 --roi 0.16,0.17 0.88,0.25 0.86,0.83 0.17,0.89 \
 *             [--inset 3] [--fps 5] [--slots 6 --slot-ms 10000] [--csv trace.csv]
 *             [--cut 0.6] [--settle-ms 600] [--min-slot-ms 4000]
 */
#include "popcore.h"
#include "stream.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static void usage(void)
{
    fprintf(stderr,
            "usage: popreplay VIDEO --roi x,y x,y x,y x,y [--inset PCT] [--fps N]\n"
            "                 [--slots N --slot-ms MS] [--csv FILE] [--cut F] [--settle-ms MS]\n"
            "                 [--min-slot-ms MS]\n"
            "ROI corners are normalised 0..1: top-left, top-right, bottom-right, bottom-left.\n");
}

static void fmt_time(int64_t ms, char *b, size_t n)
{
    snprintf(b, n, "%02lld:%02lld.%03lld", (long long)(ms / 60000), (long long)(ms / 1000 % 60),
             (long long)(ms % 1000));
}

int main(int argc, char **argv)
{
    const char *video = NULL, *csv = NULL;
    pc_point roi[4] = {{0, 0}, {1, 0}, {1, 1}, {0, 1}};
    float inset = 3.f;
    int fps = 5;
    pc_profile prof = {0, 0};
    pc_detector_cfg dc;
    pc_detector_cfg_default(&dc);

    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--roi") && i + 4 < argc) {
            for (int k = 0; k < 4; k++)
                if (sscanf(argv[++i], "%f,%f", &roi[k].x, &roi[k].y) != 2) { usage(); return 2; }
        } else if (!strcmp(argv[i], "--inset") && i + 1 < argc) inset = (float)atof(argv[++i]);
        else if (!strcmp(argv[i], "--fps") && i + 1 < argc) fps = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--slots") && i + 1 < argc) prof.slot_count = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--slot-ms") && i + 1 < argc) prof.slot_ms = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--csv") && i + 1 < argc) csv = argv[++i];
        else if (!strcmp(argv[i], "--cut") && i + 1 < argc) dc.cut_fraction = (float)atof(argv[++i]);
        else if (!strcmp(argv[i], "--settle-ms") && i + 1 < argc) dc.settle_ms = atoi(argv[++i]);
        else if (!strcmp(argv[i], "--min-slot-ms") && i + 1 < argc) dc.min_slot_ms = atoi(argv[++i]);
        else if (argv[i][0] != '-' && !video) video = argv[i];
        else { usage(); return 2; }
    }
    if (!video || fps < 1) { usage(); return 2; }

    char err[256];
    pop_stream_opts so = {0, 0, 10, NULL, NULL};
    pop_stream *s = pop_stream_open(video, &so, err, sizeof err);
    if (!s) { fprintf(stderr, "cannot open %s: %s\n", video, err); return 1; }
    FILE *trace = csv ? fopen(csv, "w") : NULL;
    if (trace) fprintf(trace, "t_ms,state,cut,ref_distance,mean_y,std_y,sharpness,event\n");

    pc_detector det;
    pc_faults fm;
    pc_detector_init(&det, &dc, &prof);
    pc_faults_init(&fm, NULL, &dc, &prof);
    pc_warp warp = {0};
    int ow = 192, oh = 96;
    uint8_t y[192 * 192], u[192 * 192], v[192 * 192];
    pc_frame f;
    int64_t pts, last = -1000000;
    int plays = 0, blanks = 0, videos = 0, faults = 0;
    char tb[32];

    printf("time       event\n");
    while (pop_stream_read(s, &f, &pts) == 1) {
        if (pts - last < 1000 / fps - 5) continue;
        last = pts;
        if (!warp.lut) {
            float tw = hypotf((roi[1].x - roi[0].x) * f.width, (roi[1].y - roi[0].y) * f.height);
            float lh = hypotf((roi[3].x - roi[0].x) * f.width, (roi[3].y - roi[0].y) * f.height);
            oh = tw > 1 ? (int)lroundf(ow * lh / tw) : 96;
            if (oh < 24) oh = 24;
            if (oh > 192) oh = 192;
            if (pc_warp_init(&warp, f.width, f.height, roi, inset, ow, oh) != 0) {
                fprintf(stderr, "invalid ROI\n");
                return 1;
            }
            printf("# %s: %dx%d, board sampled at %dx%d, %d fps analysis, profile %dx%d ms\n",
                   video, f.width, f.height, ow, oh, fps, prof.slot_count, prof.slot_ms);
        }
        pc_warp_apply(&warp, &f, y, u, v);
        pc_features fe;
        pc_features_compute(y, u, v, ow, oh, &fe);
        pc_event ev;
        int fired = pc_detector_push(&det, &fe, pts, &ev);
        if (fired) {
            plays++;
            blanks += ev.is_blank;
            videos += ev.motion;
            fmt_time(ev.change_start_ms, tb, sizeof tb);
            printf("%s  PLAY #%llu creative c-%04d after %.1f s, conf %.2f%s%s\n", tb,
                   (unsigned long long)ev.sequence, ev.creative_id, ev.previous_play_ms / 1000.0,
                   ev.confidence, ev.is_blank ? " [blank]" : "", ev.motion ? " [video]" : "");
        }
        pc_faults_push(&fm, &fe, fired ? &ev : NULL, pts);
        pc_fault_event fev;
        while (pc_faults_pop(&fm, &fev)) {
            faults += fev.raised;
            fmt_time(fev.at_ms, tb, sizeof tb);
            printf("%s  FAULT %s %s: %s\n", tb, pc_fault_name(fev.type),
                   fev.one_shot ? "occurred" : (fev.raised ? "raised" : "cleared"), fev.detail);
        }
        if (trace)
            fprintf(trace, "%lld,%s,%.3f,%.2f,%.1f,%.1f,%.0f,%s\n", (long long)pts,
                    pc_state_name(det.state), det.last_cut_score, det.last_ref_distance, fe.mean_y,
                    fe.std_y, fe.sharpness, fired ? "play" : "");
    }
    fmt_time(last, tb, sizeof tb);
    printf("# %s analysed: %d plays (%d blank, %d video), %d distinct creatives, %d fault(s) raised\n",
           tb, plays, blanks, videos, det.library.count, faults);
    if (trace) fclose(trace);
    pc_warp_free(&warp);
    pop_stream_close(s);
    return 0;
}
