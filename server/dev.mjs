import express from 'express';import {readFileSync} from 'node:fs';import {parse} from 'dotenv';
// Load only Turso variables from an explicitly selected external local file.
const file=process.env.LOCAL_TURSO_ENV_FILE??'.env.local';
try{const parsed=parse(readFileSync(file));for(const key of ['TURSO_DATABASE_URL','TURSO_AUTH_TOKEN','PAPER_NAMESPACE'])if(!process.env[key]&&parsed[key])process.env[key]=parsed[key];}catch(error){if(error.code!=='ENOENT')throw new Error('Unable to read local server configuration');}
const {default:handler}=await import('../api/dashboard.js');const app=express();app.disable('x-powered-by');
app.use((_req,res,next)=>{res.setHeader('X-Content-Type-Options','nosniff');res.setHeader('Referrer-Policy','strict-origin-when-cross-origin');next();});
app.all('/api/dashboard',handler);
if(process.env.NODE_ENV==='production')app.use(express.static('dist'));else{const {createServer}=await import('vite');const vite=await createServer({server:{middlewareMode:true},appType:'spa'});app.use(vite.middlewares);}
const port=Number(process.env.PORT??5173);app.listen(port,'127.0.0.1',()=>console.log(`Dashboard ready: http://127.0.0.1:${port} (read-only; active book from config, archive paper_v1)`));
