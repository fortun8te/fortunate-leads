"""Figma tag geometry, rendered font, selection independence; isolated mock only."""
exec(open('tests/ui/helpers.py').read())
new_tab(base + '/?mock=1#/tags')
wait_for_load(); settle()
cdp('DOM.enable'); cdp('CSS.enable')
root = cdp('DOM.getDocument')['root']['nodeId']
node = cdp('DOM.querySelector', nodeId=root, selector='.tchip')['nodeId']
fonts = cdp('CSS.getPlatformFontsForNode', nodeId=node)['fonts']
assert any(f['familyName'] == 'ABC Areal Superfamily Variable' and f['glyphCount'] > 0 for f in fonts), fonts
checks.append({'check': 'Actual rendered tag glyphs use ABC Areal', 'passed': True, 'actual': fonts})
for theme in ['light', 'dark']:
    js(f'document.documentElement.dataset.theme="{theme}"')
    cdp('Emulation.setDeviceMetricsOverride', width=1440, height=1000, deviceScaleFactor=1, mobile=False)
    settle(); shot(f'tags-{theme}-1440')
    check(f'{theme}: regular tags use Figma geometry', '[...document.querySelectorAll(".tchip[data-importance]")].every(e=>getComputedStyle(e).borderRadius==="9px" && e.getBoundingClientRect().height===34)')
    check(f'{theme}: all importance levels preserve appearance when selected', '''(()=>{const host=document.createElement('div');document.body.append(host);let ok=true;for(const level of ['exceptional','priority','strong','accent','standard','quiet','background']){host.innerHTML=`<button class="tag" data-importance="${level}">Tag</button>`;const e=host.firstChild,c=getComputedStyle(e),before=[c.background,c.color,c.border].join('|');e.classList.add('is-filtered');const after=getComputedStyle(e);ok &&= before===[after.background,after.color,after.border].join('|') && after.outlineStyle==='none' && e.getBoundingClientRect().height===28;}host.remove();return ok})()''')
    cdp('Emulation.setDeviceMetricsOverride', width=390, height=1000, deviceScaleFactor=1, mobile=True)
    settle(); shot(f'tags-{theme}-390')
    check(f'{theme}: phone layout contains every group', 'document.documentElement.scrollWidth<=innerWidth && [...document.querySelectorAll(".tg-chips")].every(e=>e.scrollWidth<=e.clientWidth)')
(out/'figma-tags.json').write_text(json.dumps(checks,indent=2))
print(json.dumps({'passed':len(checks),'total':len(checks),'output':str(out)}))
