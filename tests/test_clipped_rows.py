import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from squad_inviter import Screen, Polymarket

xml = (Path(__file__).with_name('fixtures') / 'clipped-row.xml').read_text()
s = Screen(xml, 'com.polymarket.ios-app')
row = next(r for r in s.rows().values() if r.handle == 'chris.10')
assert len(s.rows()) == 10
assert s.selection_point(row) == (359.0, 769)
assert s.selection_control(row).center[1] == 780
for change in ('covered', 'wrong_identity', 'invite_overlap'):
    root = ET.fromstring(xml)
    cell = next(n for n in root.iter('XCUIElementTypeCell') if n.get('name') == row.key)
    button = next(n for n in cell.iter('XCUIElementTypeButton'))
    if change == 'covered':
        button.set('y', '770')
    elif change == 'wrong_identity':
        cell.attrib.pop('name')
    else:
        invite = next(n for n in root.iter('XCUIElementTypeButton') if n.get('label') == 'Invite 1')
        invite.set('y', '771')
    screen = Screen(ET.tostring(root, encoding='unicode'), 'com.polymarket.ios-app')
    assert 'chris.10' not in [r.handle for r in screen.rows().values()], change

class Page:
    count=20; list_view=NS(); selected=0; heading=True; app=NS(x=0,width=400)
    def __init__(self, ids): self.ids=ids
    def rows(self): return {str(i):NS(invited=False) for i in self.ids}
    def bounds(self): return 200,700
    def field(self): return NS(attributes={'value':'chris'})

class Driver:
    def __init__(self): self.moves=[]
    def touch(self,*a,**kw): self.moves.append((a,kw))

d=Driver(); m=Polymarket(d, NS(scroll_fraction=1.0, fast_swipe_drag_ms=150, fast_swipe_hold_ms=60, fast_swipe_settle=0))
pages=iter([Page(range(11,20)),Page(range(10,20))])
m.screen=lambda:next(pages)
m.search=lambda query:Page(range(10))
with patch('squad_inviter.time.sleep'):
    count, rows=m.collect('chris',screen=Page(range(10)))
assert len(rows)==count==20
assert len(d.moves)==2
assert d.moves[0][0][1:]==(698,202)
assert d.moves[0][1] == {"drag_ms": 150, "hold_ms": 60}
assert d.moves[1][1] == {}
assert d.moves[1][0][1:]==(625,410)
print('Passed exposed-edge account identity, covered/overlapping control rejection, bounded full scroll, and incomplete-coverage fallback.')

# A successful selection resets the app to the top. Keep using full drags.
d=Driver(); m=Polymarket(d, NS(scroll_fraction=1.0))
selected=0
def page(ids):
    result=Page(ids); result.selected=selected
    return result
def select_and_reset(screen):
    global selected
    selected=10 if selected==0 else selected+1
    return page(range(10))
m.screen=lambda:page(range(10,20))
m.search=lambda query:(_ for _ in ()).throw(AssertionError('Unnecessary fallback search'))
with patch('squad_inviter.time.sleep'):
    count,rows=m.collect('chris',screen=page(range(10)),on_page=select_and_reset,
                         needs_selection=lambda rows:selected<20)
assert count==len(rows)==selected==20
assert len(d.moves)==10 and all(move[0][1:]==(698,202) for move in d.moves)
print('Passed repeated full scrolls after successful selections reset the list, without unnecessary fallback.')
