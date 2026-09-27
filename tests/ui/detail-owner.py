"""Owner-derived detail readback regression, isolated mock API only."""
exec(open('tests/ui/helpers.py').read())
new_tab(base + '/?mock=1&owner-readback=1')
wait_for_load()
cdp('Emulation.setDeviceMetricsOverride', width=1440, height=1000, deviceScaleFactor=1, mobile=False)
wait_for_element('.lead-open', timeout=15)
js('document.querySelector(".lead-open").focus()')
key('Enter', 'Enter', '\r')
for _ in range(40):
    if js('!!S.person && !S.person.loading'):
        break
    time.sleep(.2)
# These fixtures emulate the backend owner projection; real marks still use the sample API.
js('''window.ownerOriginalGet=api.get; api.get=async function(url){const p=await ownerOriginalGet.call(api,url); if(/^\\/api\\/person\\/\\d+$/.test(url)){const wins=p.status !== 'no' && (p.relationships?.includes('client') || p.status === 'talking'); p.tags=wins?[]:[{tag:'Client',source:'manual',grp:'custom'},{tag:'Not reachable',source:'auto',grp:'signal'}]; p.manual_tags=['Client']; p.owner_status=p.status || (p.relationships?.includes('client') ? 'client' : null); p.owner_conflict=p.status === 'no'?'Client label conflicts with Not a fit status. Choose the current status.':null; p.reason=wins?'Owner relationship confirmed':'Earlier automatic assessment'; p.scout={verdict:'no',reachable:false,summary:'Previous research',overridden_by_owner:wins,sources:['https://example.com']};}return p};''')
try:
    for status in ['client', 'talking', 'no']:
        control = 'd-relationship-client' if status == 'client' else 'd-status-' + status
        js(f'document.querySelector("#{control}").focus()')
        key('Enter', 'Enter', '\r')
        for _ in range(40):
            if js(f'S.person.owner_status === "{status}"'):
                break
            time.sleep(.2)
        check(f'{status} readback preserves focus', f'document.activeElement.id === "{control}"')
        if status == 'no':
            check('Not a fit immediately restores conflict and manual Client label', '!!document.querySelector(".d-owner-conflict") && !!document.querySelector(".d-manual-tags [data-rmtag=Client]") && document.querySelector(".d-reason").textContent === "Earlier automatic assessment"')
        else:
            check(f'{status} immediately refreshes tags, reason, conflict and research', '!document.querySelector(".d-owner-conflict") && !document.querySelector(".d-manual-tags [data-rmtag=Client]") && document.querySelector(".d-reason").textContent === "Owner relationship confirmed" && document.querySelector("details[data-detail-section=profile]").textContent.includes("Earlier research")')
        check(f'{status} updates the visible list owner fields too', f'S.rows.find(r=>r.id===S.open).owner_status === "{status}"')
finally:
    (out / 'owner-readback-checks.json').write_text(json.dumps(checks, indent=2))
    print(json.dumps(checks, indent=2))
