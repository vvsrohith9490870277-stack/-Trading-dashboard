// Long-lived read/write helper for the paper worker: keeps one Turso client (and its connection) open.
// Protocol: one JSON request per stdin line {id,action:'query'|'batch'|'ping',sql,args|statements};
// one JSON response per stdout line {id,ok:true,result} or {id,ok:false,code,type,transient,phase,detail}.
// Only 'query' and 'batch' are served, with the same client calls and result shapes as db.mjs;
// fills, decisions, policy registration and fee estimates always stay on the per-call db.mjs path.
// Exits when stdin closes (the owning worker exited), so it can never outlive its parent.
import fs from 'fs';import path from 'node:path';import readline from 'node:readline';import {fileURLToPath} from 'node:url';import {createRequire} from 'module';
const require=createRequire(import.meta.url);const{createClient}=require('@libsql/client');
const root=path.dirname(fileURLToPath(import.meta.url));const testDatabase=process.argv[2]||'';
const testRoot=path.resolve(root,'tests')+path.sep+'test_';
if(testDatabase&&(!path.resolve(testDatabase).startsWith(testRoot)||!testDatabase.endsWith('.sqlite'))){process.stderr.write(JSON.stringify({event:'helper_refused',code:'invalid_test_path'})+'\n');process.exit(2)}
const env={};if(!testDatabase){for(const l of fs.readFileSync(process.env.PAPER_ENV_FILE||'/workspace/sahil-fyers-turso/.env','utf8').split('\n')){const p=l.indexOf('=');if(p>0&&!l.startsWith('#'))env[l.slice(0,p).trim()]=l.slice(p+1).trim().replace(/^(['"])(.*)\1$/,'$2')}}
const db=createClient(testDatabase?{url:'file:'+testDatabase}:{url:env.TURSO_DATABASE_URL,authToken:env.TURSO_AUTH_TOKEN});
const rows=r=>r.rows.map(x=>Object.fromEntries(Object.entries(x).map(([k,v])=>[k,typeof v==='bigint'?Number(v):v])));
const CODE=/^[A-Z0-9_]{1,60}$/;
// Failures before the request reached Turso: safe to retry even for writes.
const CONNECT=new Set(['ECONNREFUSED','ENOTFOUND','EAI_AGAIN','ENETUNREACH','EHOSTUNREACH','UND_ERR_CONNECT_TIMEOUT']);
// Network or server-side failures that may clear on retry (reads only unless CONNECT).
const TRANSIENT=new Set([...CONNECT,'ECONNRESET','ETIMEDOUT','EPIPE','UND_ERR_SOCKET','UND_ERR_HEADERS_TIMEOUT','UND_ERR_BODY_TIMEOUT','UND_ERR_CLOSED','SERVER_ERROR','HRANA_WEBSOCKET_ERROR','HRANA_CLOSED_ERROR','STREAM_EXPIRED','SQLITE_BUSY']);
function classify(e){
 const code=CODE.test(e?.code||'')?e.code:'TURSO_OPERATION_FAILED';
 const causeCode=[e?.cause?.code,e?.cause?.cause?.code].find(c=>typeof c==='string'&&CODE.test(c))||null;
 const fetchFailed=e?.name==='TypeError'&&/fetch failed|terminated|other side closed/i.test(String(e?.message||''));
 const keys=[code,causeCode].filter(Boolean);
 const phase=keys.some(k=>CONNECT.has(k))?'connect':'unknown';
 const transient=fetchFailed||keys.some(k=>TRANSIENT.has(k))||/HTTP status 5\d\d|status 5\d\d/.test(String(e?.message||''));
 return {code,type:String(e?.name||'Error').slice(0,40),transient,phase,detail:{cause_code:causeCode,fetch_failed:fetchFailed}};
}
const out=o=>process.stdout.write(JSON.stringify(o)+'\n');
async function handle(req){
 if(req.action==='ping'){await db.execute('SELECT 1');return {}}
 if(req.action==='query')return rows(await db.execute({sql:req.sql,args:req.args||[]}));
 if(req.action==='batch'){await db.batch(req.statements,'write');return {}}
 throw Object.assign(new Error('unsupported_action'),{code:'HELPER_UNSUPPORTED_ACTION'});
}
const rl=readline.createInterface({input:process.stdin,crlfDelay:Infinity});let inflight=0,closing=false;
rl.on('line',async line=>{
 let req;try{req=JSON.parse(line)}catch{out({id:null,ok:false,code:'HELPER_BAD_REQUEST',type:'SyntaxError',transient:false,phase:'connect'});return}
 inflight++;
 try{out({id:req.id,ok:true,result:await handle(req)})}
 catch(e){out({id:req.id,ok:false,...classify(e)})}
 finally{inflight--;if(closing&&!inflight)finish()}
});
function finish(){try{db.close()}catch{}process.exit(0)}
rl.on('close',()=>{closing=true;if(!inflight)finish()});
process.stderr.write(JSON.stringify({event:'helper_ready',pid:process.pid,test:!!testDatabase})+'\n');
