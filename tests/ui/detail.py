"""Compact lead detail checks against isolated sample data only."""
exec(open('tests/ui/helpers.py').read())
original_check = check
def check(name, expression):
    for _ in range(40):
        try:
            if js(expression):
                break
        except RuntimeError:
            pass
        time.sleep(.2)
    original_check(name, expression)

original_cdp = cdp
def cdp(method, **params):
    return original_cdp(method, _response_timeout=20, **params)

new_tab(base + '/?mock=1&compact-detail=1')
wait_for_load()
settle()
cdp('Emulation.setDeviceMetricsOverride', width=1440, height=1000, deviceScaleFactor=1, mobile=False)
try:
    click_node('link', 'Leads')
    wait_for_element('.lead-open', timeout=15)
    js('document.querySelector(".lead-open").focus()')
    key('Enter', 'Enter', '\r')
    for _ in range(20):
        if js('!!S.person && !S.person.loading && !!document.querySelector("#workflow-summary")'):
            break
        time.sleep(.2)
    check('Core edits are visible without scrolling', 'document.querySelector("#note").getBoundingClientRect().bottom < innerHeight && !document.querySelector("details[data-detail-section=profile]").open')
    check('Empty follow-up adds no empty state or repeated status', '!document.querySelector("#workflow-summary").innerText.includes("No follow-up scheduled") && !document.querySelector("#workflow-summary").innerText.includes("Client")')
    js('document.querySelector("#d-relationship-client").focus()')
    key('Enter', 'Enter', '\r')
    check('Client save preserves selected state and keyboard focus', 'S.person.relationships.includes("client") && document.activeElement.id === "d-relationship-client" && document.querySelector("#d-relationship-client").getAttribute("aria-pressed") === "true"')
    js('document.activeElement.blur()')
    key('t', 'KeyT', 't')
    check('Label shortcut opens its editor and focuses input', 'document.activeElement.id === "tag-in" && document.querySelector("details[data-detail-section=labels]").open')
    cdp('Input.insertText', text='Met at studio')
    key('Enter', 'Enter', '\r')
    check('Manual label is saved and removable beside relationship', 'S.person.tags.some(t=>t.tag === "Met at studio" && t.source === "manual") && [...document.querySelectorAll(".d-manual-tags [data-rmtag]")].some(e=>e.dataset.rmtag === "Met at studio")')
    click_node('button', 'Remove Met at studio tag')
    check('Removing label persists', '!S.person.tags.some(t=>t.tag === "Met at studio")')
    js('document.querySelector("#note").focus()')
    cdp('Input.insertText', text='Met through Jules. Discussed summer campaign. Follow up with the brief.')
    settle()
    settle()
    check('Meaningful note saves without separate activity form', 'S.person.note === document.querySelector("#note").value && S.person.note.includes("summer campaign")')
    key('Escape', 'Escape')
    js('document.querySelector("#d-profile-summary").focus()')
    key('Enter', 'Enter', '\r')
    check('Evidence is available and auto labels cannot be removed', 'document.querySelector("details[data-detail-section=profile]").open && !!document.querySelector(".d-evidence-tags .tag") && !document.querySelector(".d-evidence-tags [data-rmtag]")')
    key('Enter', 'Enter', '\r')
    js('document.querySelector("#followup-editor-summary").focus()')
    key('Enter', 'Enter', '\r')
    click_node('button', 'Tomorrow')
    js('document.querySelector("#followup-note").focus()')
    cdp('Input.insertText', text='Send brief')
    click_node('button', 'Schedule follow-up')
    check('Scheduled follow-up stays visible with next action', 'S.person.follow_up?.note === "Send brief" && document.querySelector("#workflow-summary").innerText.includes("Send brief")')
    js('document.querySelector("details[data-detail-section=labels]").open=false; document.querySelector("#detail").scrollTop=0')
    shot('detail-desktop')
    cdp('Emulation.setDeviceMetricsOverride', width=320, height=844, deviceScaleFactor=1, mobile=True)
    settle()
    check('Phone detail does not overflow horizontally', 'document.querySelector("#detail").scrollWidth <= document.querySelector("#detail").clientWidth && document.documentElement.scrollWidth <= innerWidth')
    check('Phone note is reachable without scrolling', 'document.querySelector("#note").getBoundingClientRect().bottom < innerHeight')
    shot('detail-phone')
finally:
    (out / 'detail-checks.json').write_text(json.dumps(checks, indent=2))
    print(json.dumps(checks, indent=2))
