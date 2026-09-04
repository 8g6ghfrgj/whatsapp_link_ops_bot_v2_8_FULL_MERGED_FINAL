from pathlib import Path
R=Path(__file__).resolve().parents[1]
def t(p): return (R/p).read_text(encoding="utf-8")
def ck(n,c):
    if not c: raise AssertionError(n)
    print("OK  ",n)
def main():
    b=t("app/bot.py"); d=t("app/db.py"); k=t("app/keyboards.py"); p=t("provider/server.mjs"); w=t("app/services/wa_provider.py"); j=t("app/services/join_worker.py"); bc=t("app/services/broadcast.py")
    ck("reorganized main menu", "الروابط والتجميع" in k and "الفحص والانضمام" in k and "المهام والتقارير" in k)
    ck("audit to join", "audit_enqueue:" in b and "group_active" in b)
    ck("telegram source registry", "telegram_sources" in d and "tg_sources" in b and "channel_post" in b)
    ck("job soft delete", "hidden INTEGER" in d and "job_delete:" in b and "hide_job" in b)
    ck("manual join timings", "join_delay" in b and "batch_rest=payload.get('batch_rest')" in b and "item_delay" in j)
    ck("manual broadcast target selection", "broadcast_scope:select" in b and "broadcast_selected" in b and "selected_jids" in bc)
    ck("contact names endpoint", "/accounts/contacts" in p and "async def contacts" in w)
    ck("provider pooling cache", "self._session" in w and "_group_cache" in w)
    ck("zero timing accepted", "BROADCAST_MIN_GROUP_DELAY_SECONDS',0,0" in t("app/config.py") and "BROADCAST_MIN_REPEAT_INTERVAL_SECONDS',0,0" in t("app/config.py"))
    ck("baileys runtime selfcheck", "BAILEYS OK" in t("botctl.sh"))
    print("V2.4 STATIC CHECK PASSED")
if __name__=="__main__": main()
