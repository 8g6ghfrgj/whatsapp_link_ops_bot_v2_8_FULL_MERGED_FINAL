from pathlib import Path
R=Path(__file__).resolve().parents[1]
def text(p): return (R/p).read_text(encoding='utf-8')
def check(name,cond):
    if not cond: raise AssertionError(name)
    print('OK  ',name)

def main():
    bot=text('app/bot.py'); db=text('app/db.py'); jobs=text('app/services/jobs.py'); bc=text('app/services/broadcast.py'); join=text('app/services/join_worker.py'); col=text('app/services/collector.py')
    check('pause job control', 'request_pause' in bot and 'pause_requested' in jobs and "job_pause:" in bot)
    check('job report per task', "job_report:" in bot and 'تقرير المهمة' in bot)
    check('all accounts report', "F.data=='accounts_report'" in bot and 'التقرير الشامل لجميع الحسابات' in bot)
    check('finite repeat cycles', 'repeat_total' in db and 'broadcast_repeat_count' in bot and 'broadcast_max_repeat_cycles' in text('app/config.py'))
    check('repeat interval guardrail', 'broadcast_min_repeat_interval_seconds' in text('app/config.py') and '_sleep_interruptible' in bc)
    check('same-cycle no duplicate target', 'UNIQUE(campaign_id,target_jid)' in db)
    check('audit durable inputs', 'audit_inputs' in db and 'LEFT JOIN audit_results' in bot)
    check('audit pause signal', 'job_signal(job_id)' in bot and "st='paused'" in bot)
    check('collection pause signal', '_stop_signal' in col and "pause_requested" in col)
    check('join pause signal', 'job_signal' in join and "'paused': paused" in join)
    check('broadcast pause signal', 'job_signal' in bc and "status='paused'" in bc)
    check('watch pause/delete', 'watch_toggle:' in bot and 'watch_delete:' in bot)
    print('V2.3 STATIC CHECK PASSED')
if __name__=='__main__': main()
