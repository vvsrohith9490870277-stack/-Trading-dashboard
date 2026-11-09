// @vitest-environment node
// Isolated temporary fixture database only. Runs the adaptive read model against the REAL schema.sql columns.
import {readFileSync,mkdtempSync,rmSync} from 'node:fs';import {tmpdir} from 'node:os';import {join} from 'node:path';import {createClient} from '@libsql/client';import {afterEach,it,expect} from 'vitest';import {readAdaptiveSummary} from '../server/automation-read.mjs';
import adaptivePolicy from '../automation/paper-trading/adaptive_policy.json' with {type:'json'};
const clients=[];afterEach(()=>clients.splice(0).forEach(({db,dir})=>{db.close();rmSync(dir,{recursive:true,force:true});}));
async function fixture(){const dir=mkdtempSync(join(tmpdir(),'adaptive-read-test-'));const db=createClient({url:'file:'+join(dir,'isolated.db')});clients.push({db,dir});await db.executeMultiple(readFileSync(process.cwd()+'/automation/paper-trading/schema.sql','utf8'));return db;}
const run=db=>async(sql,args)=>(await db.execute({sql,args})).rows;
it('reads tracks, spawns, gates, calibration, probation and hash-chained logs from the real adaptive schema',async()=>{
 const db=await fixture();const ns='paper_v1',t='2026-11-09T10:00:00+00:00';
 await db.execute({sql:"INSERT INTO adaptive_spawns(namespace,spawn_id,correlation_id,strategy,track,tier,parent_version,params_json,candidate_kind,eligible_for_selection,warm_start_days,eval_window_target_days,starting_equity_gross_paise,exit_haircut_paise,starting_equity_net_haircut_paise,harness_hash,data_snapshot_hash,rng_seeds_json,decision_inputs_json,status,spawned_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",args:[ns,'sp1','sp1',3,'B','FAST_SWING','pv','{"rsi_entry":36}','CANDIDATE',1,60,45,50000000,12000,49988000,'hh','dh','[1]','{}','OBSERVATION_ONLY',t]});
 await db.execute({sql:"INSERT INTO adaptive_tracks(namespace,track,strategy,slot_index,spawn_id,status,params_json,allocated_capital_paise,cash_paise,starting_equity_net_haircut_paise,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",args:[ns,'B',3,1,'sp1','challenger','{"rsi_entry":36}',50000000,50000000,49988000,t,t]});
 await db.execute({sql:"INSERT INTO adaptive_gate_evaluations(namespace,eval_id,spawn_id,correlation_id,strategy,decision_day,k_cand,rho_bar,k_eff,effective_critical_value,t_hac,d_t_mean,residual_alpha,residual_beta,gates_json,overall_passed,status,observation_only,data_hash,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",args:[ns,'e1','sp1','sp1',3,'2026-11-09',3,0.4,2.2,1.96,2.5,0.0009,0.0008,0.1,'{"summary":{}}',1,'OBSERVATION_ONLY',1,'d',t]});
 await db.execute({sql:"INSERT INTO adaptive_calibration_runs(namespace,calibration_id,correlation_id,strategy,partition_name,sigma_d_t,mde,injected_edge,measured_power,power_floor,go_no_go,pipeline_mode,details_json,data_hash,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",args:[ns,'c1','c1',3,'OLDEST',0.001,0.0004,0.0005,0.42,0.3,'GO','OBSERVATION_ONLY_PENDING_COMPLETION','{}','d',t]});
 await db.execute({sql:"INSERT INTO adaptive_decision_logs(namespace,seq,log_id,correlation_id,spawn_id,track,strategy,category,decision_type,named_cause,conditions_json,filters_json,indicator_snapshot_json,risk_state_json,execution_details_json,data_hash,source_at,recorded_at,prev_hash,entry_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",args:[ns,1,'l1','sp1','sp1','B',3,'SPAWN','SPAWN','candidate_spawned','{}','{}','{}','{}','{"x":1}','d',t,t,'0','abc']});
 await db.execute({sql:"INSERT INTO paper_meta(namespace,key,value) VALUES(?,?,?)",args:[ns,'adaptive_ci_lookahead_passed','1']});
 // A stored flag alone never shows CI as verified (E6): only a promotion check's recorded CI result does.
 expect((await readAdaptiveSummary(run(db),ns)).ci_lookahead_passed).toBe(false);
 await db.execute({sql:"INSERT INTO adaptive_decision_logs(namespace,seq,log_id,correlation_id,spawn_id,track,strategy,category,decision_type,named_cause,conditions_json,filters_json,indicator_snapshot_json,risk_state_json,execution_details_json,data_hash,source_at,recorded_at,prev_hash,entry_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",args:[ns,2,'l2','sp1','sp1','B',3,'PROMOTION','HOLD','observation_only_mode_enabled','{"ci_lookahead_passed":true,"ci":{"passed":true,"sha":"'+'a'.repeat(40)+'"}}','{}','{}','{}','{}','d',t,t,'abc','def']});
 const a=await readAdaptiveSummary(run(db),ns);
 expect(a.read_error).toBeNull();
 expect(a.ci_lookahead_passed).toBe(true);
 expect(a.calibration_go).toBe(true);
 expect(a.power_floor_target).toBe(adaptivePolicy.power_floor);
 expect(a.tracks).toHaveLength(1);expect(a.tracks[0]).toMatchObject({track_id:'B-S3-1',tier:'B',status:'challenger',warm_start_days:60,target_window_days:45,starting_equity_paise:49988000,exit_haircut_paise:12000});
 expect(a.spawns[0]).toMatchObject({spawn_id:'sp1',track_id:'B',k_cand:1,effective_critical_t:1.4,target_window_days:45});
 expect(a.gate_evaluations[0]).toMatchObject({spawn_id:'sp1',track_id:'B',k_cand:3,effective_critical_t:1.96,all_gates_passed:1,terminal_status:'OBSERVATION_ONLY'});
 expect(a.gate_evaluations[0].residualized_mean_bps).toBeCloseTo(8,6);
 expect(a.logs[1]).toMatchObject({seq_id:1,tier:'B',event_type:'SPAWN',named_cause:'candidate_spawned',entry_hash:'abc'});
 expect(a.logs[1].payload).toEqual({x:1});
});
it('reports an uninitialized adaptive schema instead of silently pretending it is empty',async()=>{
 const dir=mkdtempSync(join(tmpdir(),'adaptive-read-empty-'));const db=createClient({url:'file:'+join(dir,'empty.db')});clients.push({db,dir});
 const a=await readAdaptiveSummary(run(db),'paper_v1');
 expect(a.read_error).toBe('adaptive_tables_not_initialized');
 expect(a.tracks).toEqual([]);
});
