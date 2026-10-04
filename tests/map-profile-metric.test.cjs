const test=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm'),fs=require('node:fs');
const element=()=>({children:[],setAttribute(){},removeAttribute(){},addEventListener(){},append(...children){this.children.push(...children);},replaceChildren(...children){this.children=children;}});
const context={MapCore:require('../web/map-core.js'),MapModel:require('../web/map-model.js'),document:{createElement:element}};
vm.runInNewContext(fs.readFileSync(require.resolve('../web/map-view.js'),'utf8'),context);
const {MapView}=context.MapViewModule;
const text=node=>typeof node==='string'?node:[node.textContent||'',...node.children.map(text)].join(' ');
const {MapSearch}=require('../web/map-search.js');
function render(person,viewMode='network'){
 const view={model:{viewMode},active:0,r:{tip:element(),results:element(),q:element()}};
 MapView.prototype.showTip.call(view,{kind:'n',it:{d:person}});
 // The result list shows the saved count beside the name, whatever the view mode.
 const search=new MapSearch({input:element(),list:element(),model:{searchReq:{cancel(){}}},doc:{createElement:element},storage:null,metric:n=>context.MapViewModule.profileMetric(n,viewMode==='shared',viewMode)});
 search.show([person],'ok');
 return [text(view.r.tip),text(search.list)];
}
test('hover and search describe saved follower counts instead of generic fit labels',()=>{
 for(const output of render({handle:'tbmango',name:'Theo Rosenberg',fit:'weak',followers:1234,status:'contacted'})){
  assert.match(output,/1,234 followers/);assert.doesNotMatch(output,/Weak|weak|fit|Unclear/);
 }
});
test('shared mode labels source audiences without calling them mutual connections',()=>{
 for(const output of render({handle:'person',fit:'strong',followers:1234,source_count:3},'shared')){
  assert.match(output,/3 source audiences/);assert.doesNotMatch(output,/mutual|Strong|fit|1,234/);
 }
});
test('missing profile counts stay unknown instead of becoming zero or a qualification verdict',()=>{
 for(const output of render({handle:'person',fit:'weak'}))assert.match(output,/Followers unknown/);
 for(const output of render({handle:'person',fit:'weak',source_count:1}))assert.match(output,/1 source audience/);
 for(const output of render({handle:'person',fit:'weak',followers:0}))assert.match(output,/0 followers/);
});
