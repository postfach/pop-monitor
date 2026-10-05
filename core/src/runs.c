/* Share-of-time aware play accounting.
 *
 * A booking can take several slots in a loop. Back to back, the same creative
 * simply stays up for 2 x S, so the screen only changes once for two slots.
 * Counting changes would call that a missed play. Instead each play earns the
 * slots it occupied (its duration rounded to whole slots), but never more than
 * that creative's usual run length: a creative that normally holds two slots
 * earns two, while a run that is longer than usual (a missed change into the
 * next creative, a hold-over, a freeze) only earns its usual length. */
#include "popcore.h"
#include <math.h>
#include <stdlib.h>
#include <string.h>

#define MAX_RUN 16

static int rounded_slots(int64_t d, int slot_ms, int cap)
{
    int r = (int)floor((double)d / slot_ms + 0.5);
    if (r < 1) r = 1;
    if (r > cap) r = cap;
    return r;
}

int pc_runs_typical(const pc_play_ref *p, int n, int slot_ms, int slot_count,
                    int *ids, int *typ, int max_ids)
{
    if (slot_ms <= 0 || n < 2) return 0;
    int cap = slot_count > 1 ? slot_count - 1 : 1;
    if (cap > MAX_RUN) cap = MAX_RUN;
    int nid = 0;
    int hist[PC_RUNS_MAX_IDS][MAX_RUN + 1];      /* 17 KB on the stack */
    if (max_ids > PC_RUNS_MAX_IDS) max_ids = PC_RUNS_MAX_IDS;
    for (int i = 0; i + 1 < n; i++) {               /* completed plays only */
        int k = -1;
        for (int j = 0; j < nid; j++) if (ids[j] == p[i].id) { k = j; break; }
        if (k < 0) {
            if (nid == max_ids) continue;
            k = nid++;
            ids[k] = p[i].id;
            memset(hist[k], 0, sizeof hist[k]);
        }
        hist[k][rounded_slots(p[i + 1].t - p[i].t, slot_ms, cap)]++;
    }
    for (int k = 0; k < nid; k++) {                 /* mode, shorter wins ties */
        int best = 1;
        for (int r = 1; r <= cap; r++) if (hist[k][r] > hist[k][best]) best = r;
        typ[k] = best;
    }
    return nid;
}

double pc_runs_credited(const pc_play_ref *p, int n, int64_t from, int64_t to,
                        int slot_ms, int slot_count, double *slots_by_id, const int *ids_in, int nids_in)
{
    if (slot_ms <= 0 || n <= 0 || to <= from) return 0;
    int cap = slot_count > 1 ? slot_count - 1 : 1;
    if (cap > MAX_RUN) cap = MAX_RUN;
    int ids[PC_RUNS_MAX_IDS], typ[PC_RUNS_MAX_IDS];
    int nid = pc_runs_typical(p, n, slot_ms, slot_count, ids, typ, PC_RUNS_MAX_IDS);
    double total = 0;
    for (int i = 0; i < n; i++) {
        int64_t s = p[i].t, e = i + 1 < n ? p[i + 1].t : to;
        if (e <= from || s >= to || e <= s) continue;
        int t = 1;
        for (int j = 0; j < nid; j++) if (ids[j] == p[i].id) { t = typ[j]; break; }
        int r = rounded_slots(e - s, slot_ms, cap);
        if (r > t) r = t;
        int64_t os = s > from ? s : from, oe = e < to ? e : to;
        double c = r * (double)(oe - os) / (double)(e - s);
        total += c;
        if (slots_by_id)
            for (int j = 0; j < nids_in; j++) if (ids_in[j] == p[i].id) { slots_by_id[j] += c; break; }
    }
    return total;
}
