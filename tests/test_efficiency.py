import contextlib
import io
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
from squad_inviter import (ACCOUNT_ROW, SELECTED_CHIP, ControlUnavailable, Journal, Polymarket, Row, Screen,
                           Stop, VisibleBatch, verify_batch, record_audited)

xml='''<XCUIElementTypeApplication x="0" y="0" width="393" height="852">
<XCUIElementTypeStaticText label="Find Friends" x="130" y="90" width="110" height="20"/>
<XCUIElementTypeTextField value="mike" x="20" y="140" width="350" height="36"/>
<XCUIElementTypeStaticText label="20 on Polymarket" x="23" y="269" width="150" height="18"/>
<XCUIElementTypeCell name="cui.listRow.contact-a" x="16" y="310" width="361" height="48"><XCUIElementTypeStaticText label="Mike" x="70" y="315" width="100" height="20"/></XCUIElementTypeCell>
<XCUIElementTypeCell name="cui.listRow.contact-b" x="16" y="358" width="361" height="48"><XCUIElementTypeStaticText label="mike" x="70" y="363" width="100" height="20"/></XCUIElementTypeCell>
<XCUIElementTypeButton name="cui.iconButton.remove-a" x="20" y="195" width="40" height="25"/>
<XCUIElementTypeButton label="Invite 1" x="25" y="775" width="343" height="47"/>
</XCUIElementTypeApplication>'''
# Removing unused XML attributes must preserve IDs, labels and positions.
import xml.etree.ElementTree as ET
root=ET.fromstring(xml)
for n in root.iter():
    n.set('type',n.tag); n.set('index','0'); n.set('traits','')
full=Screen(ET.tostring(root,encoding='unicode'),'app')
for n in root.iter():
    for key in ('type','index','traits'): n.attrib.pop(key,None)
slim=Screen(ET.tostring(root,encoding='unicode'),'app')
assert full.rows()==slim.rows() or [(k,r.handle,r.status,r.node.center) for k,r in full.rows().items()]==[(k,r.handle,r.status,r.node.center) for k,r in slim.rows().items()]
assert full.selected==slim.selected and full.selected_accounts==slim.selected_accounts
assert full.invite.center==slim.invite.center and full.field().center==slim.field().center
print('Passed lightweight XML snapshot identity, selection count and control geometry.')

class VerificationPage:
    def __init__(self, invited):
        self.count=20; self.list_view=NS(); self.selected=0
        self.heading=True; self.app=NS(x=0,width=393)
        self.invited=invited
    def rows(self):
        return {ACCOUNT_ROW+'a':Row('Mike',NS(enabled=True), 'Already invited' if self.invited else None,ACCOUNT_ROW+'a')}
    def bounds(self): return 200,750
    def field(self): return NS(attributes={'value':'mike'})
class Device:
    def __init__(self): self.swipes=0
    def touch(self,*a,**kw): self.swipes+=1
args=NS(scroll_fraction=0.60)
d=Device(); m=Polymarket(d,args)
m.screen=lambda:VerificationPage(True)
count,found=m.collect('mike',screen=VerificationPage(True),target_accounts={ACCOUNT_ROW+'a'})
assert count==20 and len(found)==1 and d.swipes==0
with patch('squad_inviter.time.sleep'):
    count,found=m.collect('mike',screen=VerificationPage(False),target_accounts={ACCOUNT_ROW+'a'})
assert d.swipes==1 and found[ACCOUNT_ROW+'a'].invited
print('Passed early verification exit only after authoritative status on every exact target ID.')

with tempfile.TemporaryDirectory() as path:
    journal=Journal(NS(state=Path(path,'state.json'),prefix='mike',squad='The Mikes',alphabet='ab'))
    journal.data['pending']=[{'account':ACCOUNT_ROW+x,'handle':'Mike','query':'mike'} for x in 'ab']
    class Grouped:
        calls=[]
        def collect(self,*a,**kw): raise AssertionError('Unnecessary group recheck')
        def wait_for_invited(self,handle,account):
            self.calls.append((handle,account))
            return True
    grouped=Grouped(); verify_batch(grouped,journal)
    assert grouped.calls==[('Mike',ACCOUNT_ROW+'a')]
    assert journal.data['pending']==[] and len(journal.data['confirmed'])==2
    assert len(Journal(NS(state=Path(path,'state.json'),prefix='mike',squad='The Mikes',alphabet='ab')).data['confirmed'])==2
    journal.data['pending']=[{'account':ACCOUNT_ROW+x,'handle':'Mike'+x} for x in 'cd']
    assert record_audited(journal,{'unrelated'})==0
    assert len(journal.data['pending'])==2
    assert record_audited(journal,{'Mikec'})==1
    assert journal.data['pending']==[] and len(journal.data['confirmed'])==4
    journal.data['pending']=[{'account':ACCOUNT_ROW+x,'handle':'Mike'+x} for x in 'efg']
    class LaterConfirmation:
        def __init__(self): self.calls=[]
        def wait_for_invited(self,handle,account):
            self.calls.append(account)
            return account==ACCOUNT_ROW+'f'
    later=LaterConfirmation(); verify_batch(later,journal)
    assert later.calls==[ACCOUNT_ROW+'e',ACCOUNT_ROW+'f']
    assert journal.data['pending']==[] and len(journal.data['confirmed'])==7
print('Passed whole-batch acceptance from one matching receipt or exact account confirmation, persisted recovery, and early exit.')

# A short initial delay must not accept a previous query's blank display.
class Clock:
    now=0.0
    def sleep(self,seconds): self.now+=seconds
    def time(self): return self.now
for remains_empty in (False,True):
    clock=Clock()
    class SearchDevice:
        changed=False
        def change_text(self,field,query): self.changed=True
    d=SearchDevice(); m=Polymarket(d,NS(search_delay=0.15))
    query_reads=[]
    def screen():
        if d.changed: query_reads.append(clock.now)
        count=0 if not d.changed or remains_empty or clock.now < 1.0 else 1
        return NS(heading=True,keyboard=None,count=count,
                  field=lambda:NS(attributes={'value':'mike' if d.changed else 'mikezz'}),
                  rows=lambda:{} if count==0 else {'a':Row('mike',NS(enabled=True),None,'a')})
    m.screen=screen
    with patch('squad_inviter.time.monotonic',clock.time),patch('squad_inviter.time.sleep',clock.sleep):
        result=m.search('mike')
    assert result.count == (0 if remains_empty else 1)
    assert len(query_reads)==3 and query_reads[0]<1.0<=query_reads[1]<query_reads[2]
    if remains_empty: assert clock.now >= 1.0 + 0.10 - 1e-9
print('Passed 0.15-second search with stale blank frames and fresh nonempty/empty results.')


# An early blank is a candidate only. A slow positive result still wins.
for positive_at in (1.08, float("inf")):
    durations = []
    for observe_after in (None, 0.75):
        clock = Clock()
        started = [None]

        class TimedInput:
            def change_text(self, field, query):
                started[0] = clock.now

        m = Polymarket(TimedInput(), NS(search_delay=0.15, empty_search_delay=1.15,
                                       empty_observation_delay=observe_after))
        reads = []

        def screen():
            clock.sleep(0.30)
            m._snapshot_seconds = 0.30
            if started[0] is None:
                query, count = "mikezz", 0
            else:
                query = "mike"
                age = clock.now - started[0]
                count = int(age >= positive_at)
                reads.append((age, count))
            rows = {"mike-id": Row("mike", NS(enabled=True), None, "mike-id")} if count else {}
            return NS(heading=True, keyboard=None, count=count, field=lambda: NS(attributes={"value": query}),
                      rows=lambda: rows)

        m.screen = screen
        with patch("squad_inviter.time.monotonic", clock.time), patch("squad_inviter.time.sleep", clock.sleep):
            result = m.search("mike")
        assert result.count == int(positive_at < float("inf"))
        assert reads[-1][0] >= 1.15
        durations.append(reads[-1][0])
    if positive_at == float("inf"):
        assert durations[1] < durations[0]
print("Passed earlier blank observation with later confirmation and no stale-blank pruning.")

# A zero count or an underlying chat title must not bypass picker dismissal.
control=NS(kind='XCUIElementTypeButton')
initial=NS(keyboard=None,invite=control,selected=1)
transitions=[
 NS(heading=True,selected=0,exact=lambda _:True),
 NS(heading=False,selected=0,exact=lambda _:False),
 NS(heading=False,selected=0,exact=lambda _:True,control=lambda _:None),
 NS(heading=False,selected=0,exact=lambda _:True,control=lambda _:control),
 NS(heading=False,selected=0,exact=lambda _:True,control=lambda _:control),
]
class SubmitDevice:
    clicks=0
    checks=0
    def tap_control(self,c): assert c is control; self.clicks+=1
    def control_hittable(self,c):
        assert c is control
        self.checks+=1
        return self.checks==2
clock=Clock(); d=SubmitDevice()
m=Polymarket(d,NS(squad='The Mikes',open_via=['invite']))
m.check_active_app=lambda:None
frames=iter([initial]+transitions)
m.screen=lambda:next(frames)
with patch('squad_inviter.time.monotonic',clock.time),patch('squad_inviter.time.sleep',clock.sleep):
    assert m.submit(['mikex'])==set()
assert d.clicks==1 and d.checks==2
assert abs(clock.now-.4)<1e-9
print('Passed post-submit readiness through count clearing, dismissal, wrong Squad and covered control; no fixed 1.5s wait.')

# Returning to the invite menu covers the chat button, but Find Friends is ready.
chat_control=NS(name='chat'); menu_control=NS(name='find')
class MenuDevice:
    checked=[]
    def control_hittable(self,c):
        self.checked.append(c.name)
        return c is menu_control
m=Polymarket(MenuDevice(),NS(squad='The Mikes',open_via=['invite','find']))
s=NS(heading=False,exact=lambda _:True,control=lambda label:menu_control if label=='find' else chat_control)
m.screen=lambda:s
assert m.wait_for_invite_route() is s
assert m.driver.checked==['find']
print('Passed return-to-invite-menu readiness while the underlying chat invite button is covered.')

# Loading, wrong-query and wrong-screen frames break the stability sequence.
for interruption in ('loading','wrong_query','wrong_screen'):
    clock=Clock()
    class InputDevice:
        def change_text(self,field,query):return False
    row=Row('mikeq',NS(enabled=True),None,'q')
    def frame(value='mikeq',count=1,heading=True):
        return NS(heading=heading,keyboard=None,count=count,
                  field=lambda:NS(attributes={'value':value}),rows=lambda:{'q':row})
    invalid=frame(count=None) if interruption=='loading' else frame(value='mikez') if interruption=='wrong_query' else frame(heading=False)
    frames=iter([frame(value='mikez',count=0),frame(),invalid,frame(),frame()])
    m=Polymarket(InputDevice(),NS(search_delay=.15))
    m.screen=lambda:next(frames)
    with patch('squad_inviter.time.monotonic',clock.time),patch('squad_inviter.time.sleep',clock.sleep):
        assert m.search('mikeq').count==1
    assert next(frames,None) is None,interruption
print('Passed consecutive stability checks across loading, wrong-query and wrong-screen interruptions.')

# Input context expires and is invalidated even when a command fails.
from squad_inviter import Appium
clock=Clock();driver=Appium.__new__(Appium)
driver.ui_revision=1;driver.session='/session/test';driver.request=lambda *a:None
m=Polymarket(driver,NS(bundle_id='app'))
snapshot=Screen(xml,'app');reads=[]
def fresh():
    reads.append(1)
    m._input_context=(snapshot,clock.time(),driver.ui_revision)
    return snapshot
m.screen=fresh
with patch('squad_inviter.time.monotonic',clock.time):
    assert m.search_context() is snapshot and len(reads)==1
    assert m.search_context() is snapshot and len(reads)==1
    driver.post('/appium/settings', {'settings': {'animationCoolOffTimeout': 0}})
    assert m.search_context() is snapshot and len(reads)==1
    driver.post('/element/field/value',{'text':'mike'})
    m.search_context();assert len(reads)==2
    clock.now=1.1
    m.search_context();assert len(reads)==3
    def fail(*a):raise Stop('interrupted command')
    driver.request=fail
    try:driver.post('/actions',{})
    except Stop:pass
    m.search_context();assert len(reads)==4
    m._input_context=(NS(heading=False),clock.time(),driver.ui_revision)
    m.search_context();assert len(reads)==5
print('Passed short-lived input context reuse, command/failure invalidation, expiration and wrong-view rejection.')

# Refocusing a closed keyboard may place the caret unexpectedly; replace and verify.
clock=Clock()
class EditingDevice:
    value='mikeq136'
    edits=[]
    def change_text(self,field,text,incremental=False):
        self.edits.append(incremental)
        self.value='wrongcaret' if incremental else text
        return incremental
editing=EditingDevice();m=Polymarket(editing,NS(search_delay=.15,incremental_search=True))
def editing_screen():
    return NS(heading=True,keyboard=None,count=1,
              field=lambda:NS(attributes={'value':editing.value}),
              rows=lambda:{'q':Row(editing.value,NS(enabled=True),None,'q')})
m.screen=editing_screen
with patch('squad_inviter.time.monotonic',clock.time),patch('squad_inviter.time.sleep',clock.sleep):
    result=m.search('mikeq')
assert editing.edits==[True,False] and result.field().attributes['value']=='mikeq'
print('Passed suffix editing with a closed keyboard and exact-text full-replacement recovery.')

# Keep the same verified geometry only when the complete result list fits.
picker_fixture = Path(__file__).with_name("fixtures") / "search-picker.xml"
root = ET.parse(picker_fixture).getroot()
app = next(root.iter("XCUIElementTypeApplication"))
keyboard = ET.SubElement(app, "XCUIElementTypeKeyboard", x="0", y="583", width="402", height="291")
bundle = app.attrib["bundleId"]
chosen = next(iter(Screen(ET.tostring(root), bundle).rows()))
ET.SubElement(app, "XCUIElementTypeButton", name=chosen.replace(ACCOUNT_ROW, SELECTED_CHIP),
              x="22", y="210", width="45", height="30")
visible = Screen(ET.tostring(root), bundle)
assert visible.invite is None and visible.selected == 1 and visible.selected_accounts == {chosen}
assert visible.results_fit_above_keyboard()
keyboard.set("y", "535")
covered = Screen(ET.tostring(root), bundle)
assert not covered.results_fit_above_keyboard()
print("Passed exact chip count with a hidden Invite button and keyboard coverage fallback.")

return_key = ET.SubElement(keyboard, "XCUIElementTypeButton", label="return",
                           x="300", y="760", width="100", height="46")
assert Screen(ET.tostring(root), bundle).keyboard_return() is not None
keyboard.set("y", "820")
return_key.set("y", "910")
partly_visible = Screen(ET.tostring(root), bundle)
assert partly_visible.keyboard and partly_visible.keyboard_return() is None
keyboard.set("y", "891")
assert Screen(ET.tostring(root), bundle).keyboard is None
print("Passed dismissal detection for an off-screen iOS keyboard and Return key.")

# Reuse a verified return screen once, with a fresh lookup if the control vanished.
for moved in (False, True):
    navigation = []
    clock = Clock()
    control = NS()
    ready = NS(heading=False, exact=lambda _: True, control=lambda _: control)
    picker = NS(heading=True, exact=lambda title: title == "The Mikes")

    def tap(control):
        navigation.append("lookup")
        if moved and navigation.count("lookup") == 1:
            raise ControlUnavailable("The cached control moved")

    market = Polymarket(NS(ui_revision=1, tap_control=tap),
                        NS(squad="The Mikes", open_via=["invite"], verify_squad=True))
    market._ready_invite_route = (ready, 0.0, 1)
    market.check_active_app = lambda: None
    frames = iter([ready, picker] if moved else [picker])
    market.screen = lambda: next(frames)
    with patch("squad_inviter.time.monotonic", clock.time):
        assert market.open_picker() is picker
    assert market._ready_invite_route is None
    assert len(navigation) == (2 if moved else 1)
    assert next(frames, None) is None
print("Passed one-use post-submit screen reuse and refreshed lookup for a moved control.")

# A complete page can fit even when its displayed count exceeds the free slots.
rows = {
    name: Row(name, NS(enabled=True), status)
    for name, status in (
        ("mike-new", None), ("mike-saved", None), ("mike-staged", None),
        ("mike-invited", "Already invited"), ("not-mike", None),
    )
}
for shown_count, confirmed, expected_flush in ((5, ["mike-saved"], False), (6, ["mike-saved"], True),
                                              (5, [], True)):
    calls = []
    page = NS(count=shown_count, rows=lambda: rows)
    market = NS(search=lambda query: calls.append(query) or page,
                collect=lambda query, screen, **_: (screen.count, screen.rows()))
    batch = VisibleBatch(market, NS(data={"confirmed": confirmed}), 2)
    batch.entries["mike-staged"] = {}
    batch.flush = lambda: calls.append("flush")
    assert batch.collect("mike") == (shown_count, rows)
    assert calls == (["mike", "flush", "mike"] if expected_flush else ["mike"])
print("Passed batch headroom checks for eligible, partial and over-capacity result pages.")

# A truncated picker without its title must be reread at full depth before use.
source = picker_fixture.read_text()
settings = []
driver = Appium.__new__(Appium)
driver.get = lambda route: {"snapshotMaxDepth": 50}
driver.post = lambda route, body: settings.append(body["settings"]["snapshotMaxDepth"])
driver.set_picker_snapshot(True)
frames = iter([source, source.replace("The Mikes", "Other chat"), source])
driver.get = lambda route: next(frames)
market = Polymarket(driver, NS(bundle_id="com.polymarket.ios-app", squad="The Mikes", verify_squad=True,
                              shallow_picker_reads=True))
assert market._allow_shallow_picker
assert market.screen().exact("The Mikes") and driver.picker_snapshot
assert market.screen().exact("The Mikes") and not driver.picker_snapshot
assert not market._allow_shallow_picker and settings == [22, 50]
assert next(frames, None) is None
print("Passed shallow picker reads and full-depth fallback when the title is missing.")
