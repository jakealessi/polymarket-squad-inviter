import contextlib,copy,io,json,tempfile
from pathlib import Path
from types import SimpleNamespace as NS
from squad_inviter import DEFAULT_ALPHABET,Journal,Row,Stop,alphabet_key,continuation_suffixes,discover
assert DEFAULT_ALPHABET=='0123456789._abcdefghijklmnopqrstuvwxyz-'
assert sorted(['mikea','mike_','mike.','mike9','mike0'],key=lambda q:alphabet_key(q,DEFAULT_ALPHABET))==['mike0','mike9','mike.','mike_','mikea']
with tempfile.TemporaryDirectory() as directory,contextlib.redirect_stdout(io.StringIO()):
 args=NS(state=Path(directory,'run.json'),prefix='mike',squad='The Mikes',alphabet=DEFAULT_ALPHABET)
 class Market:
  queries=[]
  def collect(self,q):
   self.queries.append(q)
   names=[q+str(i).zfill(2) for i in range(20)] if q in ('mike','mike0') else []
   return len(names),{x:Row(x,NS(enabled=True),None) for x in names}
 market=Market();journal=Journal(args);discover(market,journal)
 assert market.queries==['mike','mike0']+['mike0'+c for c in DEFAULT_ALPHABET]+['mike'+c for c in DEFAULT_ALPHABET[1:]]
 # Resume an old-order checkpoint, retaining all invitations and pending work.
 d=copy.deepcopy(journal.data)
 d['identity']['alphabet']='abcdefghijklmnopqrstuvwxyz0123456789_.-'
 d['remaining']=['mikea','mike_','mike.','mike9','mike0b','mike00']
 d['confirmed']=['account-done'];d['pending']=[{'account':'account-pending','handle':'mikepending'}]
 args.state.write_text(json.dumps(d))
 migrated=Journal(args)
 expected=sorted(d['remaining'],key=lambda q:alphabet_key(q,DEFAULT_ALPHABET))
 assert list(reversed(migrated.data['remaining']))==expected
 assert migrated.data['identity']=={'prefix':args.prefix,'squad':args.squad,'alphabet':DEFAULT_ALPHABET}
 for key in d.keys()-{'identity','remaining'}:assert migrated.data[key]==d[key],key
 assert json.loads(args.state.read_text())['identity']['alphabet']==DEFAULT_ALPHABET
 # Changing character coverage or Squad is still refused.
 for changes in ({'alphabet':DEFAULT_ALPHABET[:-1]},{'squad':'Another Squad'}):
  try:Journal(NS(**(vars(args)|changes)))
  except Stop:pass
  else:raise AssertionError('Non-order identity changes must be rejected')
print('Passed nested digit/dot/underscore/letter traversal, fallback sorting and order-only resume without lost progress.')


class SingleCharacterMarket:
    def __init__(self):
        self.queries = []

    def collect(self, query):
        self.queries.append(query)
        if query == "ya":
            handles = [f"other{i}" for i in range(20)]
        else:
            handles = {"y0": ["y0account"], "yaa": ["yaaccount"]}.get(query, [])
        rows = {handle: Row(handle, NS(enabled=True), None) for handle in handles}
        return len(rows), rows


with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
    args = NS(state=Path(directory, "run.json"), prefix="y", squad="The Y Squad", alphabet="0a_-")
    market = SingleCharacterMarket()
    journal = Journal(args)
    discover(market, journal, ordered=True)
    assert market.queries == ["y", "y0", "ya", "ya0", "yaa", "ya_", "ya-", "y_", "y-"]
    assert set(journal.data["candidates"]) == {"y0account", "yaaccount"}
    assert not journal.data["remaining"] and not journal.data["unresolved"]
print("Passed expansion of empty single-character searches and capped pages with no matching rows.")
