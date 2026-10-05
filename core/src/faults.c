/* Fault monitor: black, blank, frozen, blank slot, loop, partial, blur. */
#include "popcore.h"
#include <stdio.h>
#include <string.h>

void pc_faults_cfg_default(pc_faults_cfg *c)
{
    c->black_ms = 10000;
    c->blank_ms = 10000;
    c->partial_ms = 300000;
    c->frozen_default_ms = 60000;
    c->blur_ratio = 0.35f;
    c->blur_window = 6;
}

const char *pc_fault_name(pc_fault_type t)
{
    static const char *n[PC_FAULT_COUNT] = {
        "black", "blank", "frozen", "blank_slot", "loop_changed",
        "missing_creative", "partial_failure", "blurred"};
    return (t >= 0 && t < PC_FAULT_COUNT) ? n[t] : "unknown";
}

void pc_faults_init(pc_faults *m, const pc_faults_cfg *cfg,
                    const pc_detector_cfg *dcfg, const pc_profile *p)
{
    memset(m, 0, sizeof *m);
    if (cfg) m->cfg = *cfg; else pc_faults_cfg_default(&m->cfg);
    if (dcfg) m->dcfg = *dcfg; else pc_detector_cfg_default(&m->dcfg);
    m->operating = 1;
    m->start_ms = -1;
    pc_faults_set_profile(m, p);
}

void pc_faults_set_profile(pc_faults *m, const pc_profile *p)
{
    if (p) m->profile = *p; else memset(&m->profile, 0, sizeof m->profile);
    pc_profile_triggers(&m->profile, &m->trig);
    m->loop_bad_checks = 0;
}

static void emit(pc_faults *m, pc_fault_type t, int raised, int one_shot,
                 int64_t since, int64_t at, const char *detail)
{
    if (m->qn >= PC_FAULT_QUEUE) return;
    pc_fault_event *e = &m->q[m->qn++];
    memset(e, 0, sizeof *e);
    e->type = t;
    e->raised = raised;
    e->one_shot = one_shot;
    e->since_ms = since;
    e->at_ms = at;
    if (detail) snprintf(e->detail, sizeof e->detail, "%s", detail);
}

/* Persistent condition with a hold time before raising. */
static void track(pc_faults *m, pc_fault_type t, int cond, int hold_ms,
                  int64_t now, const char *detail)
{
    if (cond) {
        if (!m->cond_since[t]) m->cond_since[t] = now;
        if (!m->active[t] && now - m->cond_since[t] >= hold_ms) {
            m->active[t] = 1;
            m->raised_at[t] = now;
            emit(m, t, 1, 0, m->cond_since[t], now, detail);
        }
    } else {
        if (m->active[t]) {
            m->active[t] = 0;
            emit(m, t, 0, 0, m->cond_since[t], now, "recovered");
        }
        m->cond_since[t] = 0;
    }
}

void pc_faults_set_operating(pc_faults *m, int operating, int64_t now)
{
    if (operating && !m->operating) {
        m->last_play_ms = now;           /* don't call it frozen on switch-on */
        m->last_loop_check_ms = now;
        m->play_count = 0;
        m->loop_bad_checks = 0;
    }
    m->operating = operating;
}

static void record_play(pc_faults *m, const pc_event *ev, int64_t now)
{
    m->plays[m->play_head].t = ev->change_start_ms;
    m->plays[m->play_head].id = ev->creative_id;
    m->plays[m->play_head].blank = ev->is_blank;
    m->play_head = (m->play_head + 1) % PC_PLAY_HISTORY;
    if (m->play_count < PC_PLAY_HISTORY) m->play_count++;
    m->last_play_ms = now;
    for (int i = 0; i < PC_CELLS; i++)
        if (m->cell_dark_since[i]) m->cell_dark_plays[i]++;

    /* blur: compare this play's sharpness with the creative's first sighting */
    if (ev->is_blank || ev->motion) return;
    int k = -1;
    for (int i = 0; i < m->sharp_ref_n; i++)
        if (m->sharp_ref[i].id == ev->creative_id) { k = i; break; }
    if (k < 0) {
        int slot = m->sharp_ref_n < 64 ? m->sharp_ref_n++ : (ev->creative_id % 64);
        m->sharp_ref[slot].id = ev->creative_id;
        m->sharp_ref[slot].sharp = ev->fp.sharpness;
        return;
    }
    float ref = m->sharp_ref[k].sharp;
    int low = ref > 1.f && ev->fp.sharpness < m->cfg.blur_ratio * ref;
    int W = m->cfg.blur_window > 0 && m->cfg.blur_window <= 32 ? m->cfg.blur_window : 6;
    m->blur_hist[m->blur_head] = (uint8_t)low;
    m->blur_head = (m->blur_head + 1) % W;
    if (m->blur_n < W) m->blur_n++;
    int nlow = 0;
    for (int i = 0; i < m->blur_n; i++) nlow += m->blur_hist[i];
    if (m->blur_n >= W) {
        if (!m->active[PC_FAULT_BLURRED] && nlow * 3 >= W * 2) {
            m->active[PC_FAULT_BLURRED] = 1;
            m->cond_since[PC_FAULT_BLURRED] = now;
            emit(m, PC_FAULT_BLURRED, 1, 0, now, now,
                 "board much softer than usual: camera moved, defocused, fogged or dirty");
        } else if (m->active[PC_FAULT_BLURRED] && nlow * 6 <= W) {
            m->active[PC_FAULT_BLURRED] = 0;
            emit(m, PC_FAULT_BLURRED, 0, 0, m->cond_since[PC_FAULT_BLURRED], now, "recovered");
        }
    }
}

static void loop_check(pc_faults *m, int64_t now)
{
    int N = m->profile.slot_count, L = m->trig.loop_ms;
    if (N <= 1 || L <= 0) return;
    if (now - m->start_ms < 2LL * L) return;            /* need history     */
    if (now - m->last_loop_check_ms < L) return;
    m->last_loop_check_ms = now;

    int count = 0, ids[PC_PLAY_HISTORY], nid = 0;
    for (int i = 0; i < m->play_count; i++) {
        int k = (m->play_head - 1 - i + PC_PLAY_HISTORY) % PC_PLAY_HISTORY;
        if (now - m->plays[k].t > 2LL * L) break;
        count++;
        if (m->plays[k].blank) continue;
        int seen = 0;
        for (int j = 0; j < nid; j++) if (ids[j] == m->plays[k].id) { seen = 1; break; }
        if (!seen) ids[nid++] = m->plays[k].id;
    }
    char d[160];
    int loop_bad = count < 2 * N - 1 || count > 2 * N + 1;
    snprintf(d, sizeof d, "%d plays in last 2 loops, expected %d (%d slots x %d ms)",
             count, 2 * N, N, m->profile.slot_ms);
    /* Require two consecutive bad checks; clear on the first good one. */
    if (loop_bad) m->loop_bad_checks++; else m->loop_bad_checks = 0;
    track(m, PC_FAULT_LOOP_CHANGED, m->loop_bad_checks >= 2, 0, now, d);

    snprintf(d, sizeof d, "%d distinct creatives in last 2 loops, expected %d", nid, N);
    track(m, PC_FAULT_MISSING_CREATIVE, nid < N && !loop_bad, 0, now, d);
}

int pc_faults_push(pc_faults *m, const pc_features *f, const pc_event *ev, int64_t now)
{
    if (m->start_ms < 0) {
        m->start_ms = now;
        m->last_play_ms = now;
        m->last_loop_check_ms = now;
    }
    int black;
    int blank = pc_is_blank(&m->dcfg, f, &black);
    int on = m->operating;

    track(m, PC_FAULT_BLACK, on && black, m->cfg.black_ms, now, "screen is black or off");
    track(m, PC_FAULT_BLANK, on && blank && !black, m->cfg.blank_ms, now,
          "screen shows a uniform colour (no signal / blank)");

    /* blank slot: a blank run that lasted about one slot */
    if (blank) {
        if (!m->blank_since) m->blank_since = now;
    } else if (m->blank_since) {
        int64_t dur = now - m->blank_since;
        if (on && dur >= m->trig.blank_slot_min_ms && dur <= m->trig.blank_slot_max_ms) {
            char d[96];
            snprintf(d, sizeof d, "blank for %lld ms in the rotation", (long long)dur);
            emit(m, PC_FAULT_BLANK_SLOT, 1, 1, m->blank_since, now, d);
        }
        m->blank_since = 0;
    }

    if (ev && ev->type == PC_EV_NEW_PLAY) record_play(m, ev, now);

    int frozen_ms = m->profile.slot_count == 1 ? 0          /* takeover */
                  : m->profile.slot_ms > 0 ? m->trig.frozen_ms : m->cfg.frozen_default_ms;
    char d[96];
    snprintf(d, sizeof d, "no creative change for over %d s", frozen_ms / 1000);
    track(m, PC_FAULT_FROZEN, on && !blank && frozen_ms > 0 && now - m->last_play_ms >= frozen_ms,
          0, now, d);

    /* partial failure: some cells dark while the board is clearly on */
    int dark_cells = 0;
    int64_t oldest = 0;
    int board_on = !blank && f->mean_y > m->dcfg.black_mean * 3;
    for (int i = 0; i < PC_CELLS; i++) {
        if (board_on && f->cell_y[i] < m->dcfg.black_mean) {
            if (!m->cell_dark_since[i]) m->cell_dark_since[i] = now;
            /* dark across several creatives, not just a dark creative */
            if (now - m->cell_dark_since[i] >= m->cfg.partial_ms &&
                m->cell_dark_plays[i] >= 3) {
                dark_cells++;
                if (!oldest || m->cell_dark_since[i] < oldest) oldest = m->cell_dark_since[i];
            }
        } else if (board_on || !on) {
            m->cell_dark_since[i] = 0;
            m->cell_dark_plays[i] = 0;
        }
    }
    snprintf(d, sizeof d, "%d of %d board zones dark for over %d s", dark_cells, PC_CELLS,
             m->cfg.partial_ms / 1000);
    track(m, PC_FAULT_PARTIAL, on && dark_cells > 0, 0, now, d);

    if (on) loop_check(m, now);
    return m->qn;
}

int pc_faults_pop(pc_faults *m, pc_fault_event *out)
{
    if (m->qn == 0) return 0;
    *out = m->q[0];
    memmove(&m->q[0], &m->q[1], sizeof(m->q[0]) * (size_t)(m->qn - 1));
    m->qn--;
    return 1;
}
