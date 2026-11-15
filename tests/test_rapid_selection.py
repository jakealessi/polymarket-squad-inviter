import contextlib
from types import SimpleNamespace as NS

from squad_inviter import ACCOUNT_ROW, Appium, Polymarket, Row, Stop, VisibleBatch

rows=[Row('chris'+str(i),NS(enabled=True),None,ACCOUNT_ROW+str(i)) for i in range(3)]
base={ACCOUNT_ROW+'anchor'}
targets={r.key for r in rows}
class Screen:
    heading=True; list_view=NS()
    def __init__(self,selected, visible=None):
        self.selected_accounts=set(selected); self.selected=len(selected)
        self.visible=rows if visible is None else visible
    def field(self):return NS(attributes={'value':'chris'})
    def rows(self):return {r.key:r for r in self.visible}
    def selection_control(self,row):return NS(attributes={'value':'0'})
    def selection_point(self,row,control=None):return (350,300+rows.index(row)*52)
class Driver:
    def __init__(self):self.taps=[]
    def tap_event(self,points):self.taps.extend(points)

driver=Driver();market=Polymarket(driver,NS(independent_touch_events=True));reads=[]
def snapshot():reads.append(1);return Screen(base|targets)
market.screen=snapshot
result=market.select_group(Screen(base),rows,1)
assert result.selected_accounts==base|targets and len(driver.taps)==3 and len(reads)==1

# A shortened wait can return while the final chip is still appearing.
driver.selection_timing = lambda: contextlib.nullcontext(True)
frames = iter([Screen(base | {rows[0].key}), Screen(base | targets)])
market.screen = lambda: next(frames)
assert market.select_group(Screen(base), rows, 1).selected_accounts == base | targets
assert next(frames, None) is None
del driver.selection_timing

# A partial success retries only the absent IDs using fresh individual checks.
driver.taps.clear();current=Screen(base|{rows[0].key});retried=[]
market.screen=lambda:current
def retry(screen,row,expected):
    retried.append(row.key)
    return Screen(screen.selected_accounts|{row.key})
market.select=retry
result=market.select_group(Screen(base),rows,1)
assert retried==[r.key for r in rows[1:]] and result.selected_accounts==base|targets

# Same total count with a wrong ID must stop; no broad count-only acceptance.
market.screen=lambda:Screen(base|{rows[0].key,rows[1].key,ACCOUNT_ROW+'wrong'})
try:market.select_group(Screen(base),rows,1)
except Stop:pass
else:raise AssertionError('Unexpected ID accepted')

driver.taps.clear();invalid=Screen(base)
invalid.selection_point=lambda row,control=None:None if row==rows[-1] else (350,rows.index(row)*52+300)
try:market.select_group(invalid,rows,1)
except Stop:pass
else:raise AssertionError('Missing geometry accepted')
assert not driver.taps

# The caller batches only after a checked tap leaves the first row unchanged.
for reset in (False,True):
    journal=NS(data={'confirmed':[],'candidates':{},'origins':{}},save=lambda:None)
    calls=[]
    class Market:
        def select(self,screen,row,expected):
            calls.append(('single',row.key))
            return Screen(screen.selected_accounts|{row.key},rows[1:] if reset else rows)
        def select_group(self,screen,group,expected):
            assert all(r.key in journal.data['candidates'] for r in group)
            calls.append(('group',len(group)))
            return Screen(screen.selected_accounts|{r.key for r in group},rows)
    batch=VisibleBatch(Market(),journal,3)
    batch.select_page(Screen(set(),rows),'chris')
    assert len(batch.entries)==3
    if not reset:assert calls==[('single',rows[0].key),('group',2)]
    else:assert not any(c[0]=='group' for c in calls)
print('Passed one-check tap groups, missing-only retry, exact ID rejection, preflight geometry, checkpointing, capacity and viewport-reset handling.')

# Restore the active cooldown even when a tap raises.
for fails in (False, True):
    driver = Appium.__new__(Appium)
    events = []
    driver.get = lambda route: {"animationCoolOffTimeout": 0.75}
    driver.post = lambda route, body: events.append(body["settings"]["animationCoolOffTimeout"])

    def tap(x, y):
        events.append((x, y))
        if fails:
            raise Stop("test tap failure")

    driver.tap = tap
    try:
        driver.tap_sequence([(10, 20), (30, 40)])
    except Stop:
        assert fails
    expected = [0, (10, 20)] + ([] if fails else [(30, 40)]) + [0.75]
    assert events == expected
print("Passed scoped tap cooldown and restoration on failure.")

driver = Appium.__new__(Appium)
driver.get = lambda route: {"animationCoolOffTimeout": 2}
settings = []
driver.post = lambda route, body: settings.append(body["settings"]["animationCoolOffTimeout"])
try:
    with driver.selection_timing() as shortened:
        assert shortened and settings == [0]
        raise Stop("interrupted selection")
except Stop:
    assert settings == [0, 2]
else:
    raise AssertionError("The interruption was swallowed")
print("Passed selection timing restoration and deferred checks for late chips.")

# Smaller result sets create exactly their own number of independent paths.
driver = Appium.__new__(Appium)
payloads = []
driver.post = lambda route, body: payloads.append((route, body))
for count in range(2, 11):
    coordinates = [(350.25, 300 + index * 40) for index in range(count)]
    driver.tap_event(coordinates)
    route, payload = payloads[-1]
    paths = payload["actions"]
    assert route == "/actions" and len(paths) == count
    assert len({path["id"] for path in paths}) == count
    for index, path in enumerate(paths):
        actions = path["actions"]
        assert actions[-1]["type"] == "pointerUp"
        assert [action["y"] for action in actions if action["type"] == "pointerMove"] == [300 + index * 40]
        if index:
            assert actions[0] == {"type": "pause", "duration": index * 425}
print("Passed variable-length independent touch events for two through ten rows.")
