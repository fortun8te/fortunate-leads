// Request templates contain no headers, cookies, bodies or response data.
(function(root) {
  const safeKeys = ['count', 'max_id', 'search_surface', 'query'];
  function sanitize(p) {
    if (!p || p.method !== 'GET' || p.direction !== 'followers' || !/^\d+$/.test(String(p.viewer_id || '')) ||
        typeof p.path !== 'string' || !/^\/api\/v1\/friendships\/\d+\/followers\/$/.test(p.path) ||
        p.target_id !== p.path.split('/')[4] || !Array.isArray(p.params) || p.params.length > safeKeys.length) return null;
    const used = new Set(), params = [];
    for (const pair of p.params) {
      if (!Array.isArray(pair) || pair.length !== 2) return null;
      const [key, value] = pair;
      if (!safeKeys.includes(key) || used.has(key) || typeof value !== 'string') return null;
      if (key === 'count' && !/^[1-9]\d{0,3}$/.test(value)) return null;
      if (key === 'max_id' && (value.length > 4096 || /[\x00-\x1f]/.test(value))) return null;
      if (key === 'search_surface' && !/^[a-z_]{1,80}$/.test(value)) return null;
      if (key === 'query' && (value.length > 80 || /[\x00-\x1f]/.test(value))) return null;
      used.add(key); params.push([key, value]);
    }
    if (!used.has('count') || !Number.isFinite(p.at) || p.at <= 0) return null;
    return {method:'GET',direction:'followers',viewer_id:String(p.viewer_id),target_id:p.target_id,path:p.path,params,at:p.at};
  }
  const canonical = p => JSON.stringify([p.method,p.path,p.params]);
  async function hash(p, cryptoApi = root.crypto) {
    const bytes = await cryptoApi.subtle.digest('SHA-256',new TextEncoder().encode(canonical(p)));
    return Array.from(new Uint8Array(bytes),n=>n.toString(16).padStart(2,'0')).join('');
  }
  async function prepare(task,viewerId,captures,cryptoApi=root.crypto) {
    if (!['web_modal','web_search'].includes(task.route) || task.direction !== 'followers' || String(task.viewer_id)!==String(viewerId)) throw Error('invalid_capture_task');
    if (task.route==='web_search' && (task.capped_list_fallback!==true || typeof task.query!=='string' || !task.query.trim() ||
        task.query.length>80 || /[\x00-\x1f]/.test(task.query) || !Number.isSafeInteger(task.request_limit) || task.request_limit<1 || task.request_limit>20 ||
        !Number.isSafeInteger(task.request_index) || task.request_index<0 || task.request_index>=task.request_limit)) throw Error('invalid_search_bounds');
    for (const raw of [...(Array.isArray(captures)?captures:[])].reverse()) {
      const p=sanitize(raw);
      if (!p || p.viewer_id!==String(viewerId) || p.target_id!==String(task.target_id) || Date.now()-p.at>24*60*60*1000 || p.at>Date.now()+1000 ||
          raw.shape_hash!==await hash(p,cryptoApi)) continue;
      const hasQuery=p.params.some(([key])=>key==='query');
      if (hasQuery!==(task.route==='web_search')) continue;
      const params=p.params.map(pair=>pair.slice());
      const count=params.find(([key])=>key==='count');
      if (task.page_size!=null && String(task.page_size)!==count[1]) continue; // exact observed count, never invent a modal size
      const cursor=params.find(([key])=>key==='max_id');
      if ((!task.cursor && cursor && cursor[1]) || (task.cursor && !cursor)) continue; // pagination shape must also have been observed
      if (cursor) cursor[1]=task.cursor || '';
      if (hasQuery) params.find(([key])=>key==='query')[1]=task.query;
      return {url:'https://www.instagram.com'+p.path+'?'+new URLSearchParams(params),
        recipe:{route:task.route,viewer_id:p.viewer_id,target_id:p.target_id,method:p.method,path:p.path,params,shape_hash:raw.shape_hash}};
    }
    throw Error('missing_verified_followers_capture');
  }
  root.FLFollowerCapture={sanitize,canonical,hash,prepare};
  if(typeof module!=='undefined'&&module.exports)module.exports=root.FLFollowerCapture;
})(typeof globalThis!=='undefined'?globalThis:this);
