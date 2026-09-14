from pathlib import Path
p=Path('app.py')
s=p.read_text()
s=s.replace('valid = narrative_hits == 0 and duplicates == 0 and low_confidence == 0','valid = narrative_hits == 0 and duplicates == 0 and low_confidence == 0 and continuity_anomalies == 0',1)
s=s.replace('qx1, qx2, qx3 = st.columns(3)','qx1, qx2, qx3, qx4 = st.columns(4)',1)
s=s.replace('qx3.metric("Reversal rows to review", q.get("reversal_review_rows", 0))','qx3.metric("Reversal rows to review", q.get("reversal_review_rows", 0))\n            qx4.metric("Continuity anomalies", q.get("continuity_anomalies", 0))',1)
p.write_text(s)
