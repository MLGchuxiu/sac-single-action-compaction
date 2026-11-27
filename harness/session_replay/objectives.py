import sys, re
sys.path.insert(0,'.')
from saclog import Session
INSTR=('user','goal')
def objective(txt):
    """The Objective line of a recap, whatever heading style the model used."""
    m=re.search(
        r"(?im)^[ \t]*(?:#+[ \t]*)?(?:\*\*)?[ \t]*(?:\*{0,2}1[ \t]*\.[ \t]*\*{0,2})?(?:\*\*)?[ \t]*(?:objective|目标)"
        r"\**[ \t]*[:：]?[ \t]*\**[ \t]*\n?(.*?)"
        r"(?:\n[ \t]*\n|\n[ \t]*(?:#+|\*\*|__)?[ \t]*2[.\s]|\n###|\Z)",
        txt)
    if not m: return '(no objective line)'
    return re.sub(r'\s+',' ',m.group(1)).strip()
# usage: python3 objectives.py <session.v4.jsonl>   (prints one line per logged fold)
s=Session(sys.argv[1])
instr=set(q for q in s.order if s.kind(q) in INSTR)
n=0
for q in s.order:
    e=s.events[q]
    if e['type']!='compaction/summary': continue
    n+=1
    k=q+1
    while s.events[k]['type']!='user/message': k+=1
    rng=s.events[k]['surfaceOp']
    folded=[u for u in instr if rng['startSeq']<=u<=rng['endSeq']]
    raw=e['data']['rawOutput'][0]['text']
    summ=''.join(b['text'] for b in e['data']['summary'])
    print(f"#{n:3d} seq {q:5d} turn {s.turn_of(q)} fold {rng['startSeq']:5d}-{rng['endSeq']:5d} raw={len(raw):5d} final={len(summ):5d} folded_instr={sorted(folded)}")
    print(f"      OBJ: {objective(raw)[:230]}")
