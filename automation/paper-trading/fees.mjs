import policyJson from './charge_policy.json' with {type:'json'};
import ratesJson from './charge_rates.json' with {type:'json'};
import capitalPolicy from './capital_policy.json' with {type:'json'};
export const policy=policyJson;
export const rates=ratesJson;
// The numeric rate table must belong to the active dated charge policy; a mismatch fails closed.
if(rates.charge_policy_version!==policy.version)throw Error('charge_rates_policy_mismatch');
// Exact decimal -> BigInt fraction, e.g. 0.005 -> 5/1000. Policy numbers stay exact in integer paise maths.
export function fraction(value){if(typeof value!=='number'||!Number.isFinite(value)||value<0)throw Error('policy_number_invalid');const s=String(value);const m=/^(\d+)(?:\.(\d+))?$/.exec(s);if(!m)throw Error('policy_number_invalid');const dec=m[2]||'';return {num:BigInt(m[1]+dec),den:10n**BigInt(dec.length)}}
const R=k=>({num:BigInt(rates[k].num),den:BigInt(rates[k].den)});
const div=(n,d)=>(n+d/2n)/d;
const gcd=(a,b)=>{a=a<0n?-a:a;b=b<0n?-b:b;while(b){[a,b]=[b,a%b]}return a||1n};
export function paise(price){const s=String(price);if(!/^\d+(\.\d{1,2})?$/.test(s))throw Error('price_requires_positive_paise');const [a,b='']=s.split('.');const n=BigInt(a)*100n+BigInt(b.padEnd(2,'0'));if(n<=0n||n>9007199254740991n)throw Error('price_bounds');return n;}
export function estimate({turnover,side,prior=0n,priorBuy=0n,dp=false}){
 const v=BigInt(turnover),old=BigInt(prior),oldBuy=BigInt(priorBuy);if(v<=0n||!['BUY','SELL'].includes(side))throw Error('fee_inputs');
 const part=k=>{const r=R(k);return r.num===0n?0n:div(v*r.num,r.den)};
 const f={brokerage:part('brokerage'),exchange:part('exchange'),sebi:part('sebi'),ipft:part('ipft'),clearing:part('clearing')};const cap=BigInt(rates.brokerage.cap_paise);if(f.brokerage>cap)f.brokerage=cap;
 const stt=R('stt'),unit=BigInt(rates.stt.round_to_paise);f.stt=rates.stt.sides.includes(side)?(div((old+v)*stt.num,stt.den*unit)-div(old*stt.num,stt.den*unit))*unit:0n;
 const stamp=R('stamp');f.stamp=rates.stamp.sides.includes(side)?div((oldBuy+v)*stamp.num,stamp.den)-div(oldBuy*stamp.num,stamp.den):0n;
 const gst=R('gst');f.gst=div(rates.gst.base.reduce((a,k)=>a+f[k],0n)*gst.num,gst.den);f.dp=dp?BigInt(rates.dp.charge_paise):0n;f.dp_gst=dp?BigInt(rates.dp.gst_paise):0n;
 const total=Object.values(f).reduce((a,b)=>a+b,0n);if(total>BigInt(Number.MAX_SAFE_INTEGER))throw Error('fee_bounds');return {...Object.fromEntries(Object.entries(f).map(([k,v])=>[k,Number(v)])),total:Number(total)};
}
// One statutory DP charge (charge_rates.json dp: ₹12.50 + 18% GST today) per ISIN per sell day. Shares are turnover-weighted.
// Every share except the last is floored; the last sell of that ISIN that day takes the remainder paise.
export function allocateOvernightDp(turnovers){
 const weights=turnovers.map(t=>BigInt(t));
 if(!weights.length||weights.some(t=>t<=0n))throw Error('fee_inputs');
 const total=weights.reduce((a,b)=>a+b,0n);
 const split=charge=>{let used=0n;return weights.map((t,i)=>{if(i===weights.length-1)return charge-used;const share=charge*t/total;used+=share;return share})};
 const dp=split(BigInt(rates.dp.charge_paise)),gst=split(BigInt(rates.dp.gst_paise));
 return dp.map((d,i)=>({dp:Number(d),dp_gst:Number(gst[i])}));
}
function inrFromPaise(p){const v=BigInt(p);const whole=v/100n,frac=v%100n;return Number(whole.toString()+'.'+frac.toString().padStart(2,'0'));}
// bps = base + m × (notional / (rate × ADV)), all from capital_policy.json. Base is
// slippage_base_bps_through_20cr up to slippage_tier_threshold_inr and slippage_base_bps_above_20cr above it.
// Stress multiplies the whole bps by slippage_stress_multiplier.
// The returned price is the reference rounded half-up to paise. Quantity stays on the reference price.
export function executionSlippage({advPaise,orderPaise,referencePaise,side,stress=false}){
 const adv=BigInt(advPaise),order=BigInt(orderPaise),ref=BigInt(referencePaise);
 if(adv<=0n||order<=0n||ref<=0n||!['BUY','SELL'].includes(side))throw Error('slippage_inputs');
 const tier=BigInt(capitalPolicy.slippage_tier_threshold_inr)*100n;
 const base=adv<=tier?BigInt(capitalPolicy.slippage_base_bps_through_20cr):BigInt(capitalPolicy.slippage_base_bps_above_20cr);
 const m=fraction(capitalPolicy.slippage_participation_multiplier_bps),rate=fraction(capitalPolicy.participation_rate);
 // k = m / rate as a reduced fraction (10 / 0.005 = 2000/1 today, which keeps the logged numerator/denominator unchanged).
 let kn=m.num*rate.den,kd=m.den*rate.num;const g=gcd(kn,kd);kn/=g;kd/=g;
 let num=base*adv*kd+kn*order,den=adv*kd;
 if(stress){const sm=fraction(capitalPolicy.slippage_stress_multiplier);const h=gcd(sm.num,sm.den);num*=sm.num/h;den*=sm.den/h;}
 if(num>BigInt(Number.MAX_SAFE_INTEGER)||den>BigInt(Number.MAX_SAFE_INTEGER))throw Error('slippage_bounds');
 const scale=10000n*den;const bumped=side==='BUY'?scale+num:scale-num;
 if(bumped<=0n)throw Error('slippage_bounds');
 const out=div(ref*bumped,scale);if(out<=0n||out>9007199254740991n)throw Error('slippage_bounds');
 return {price_paise:Number(out),price:inrFromPaise(out),base_bps:Number(base),bps_numerator:Number(num),bps_denominator:Number(den),stress:!!stress};
}
export const DP_TOTAL_PAISE=rates.dp.charge_paise+rates.dp.gst_paise;
// B1 (option (a), user-decided): base-only exit-slippage fallback when lagged ADV is
// unavailable. Price = reference x (1 -/+ base/10000), half-up to paise, where base is
// slippage_base_bps_through_20cr read from capital_policy.json (never typed in code).
// No participation term (it needs ADV, which is exactly what is missing) and no 1.5x
// stress (also for BE/BZ without ADV). Example: SELL 500 at 110.00 gives 109.91.
export function baseOnlySlippage({referencePaise,side}){
 const ref=BigInt(referencePaise);
 if(ref<=0n||!['BUY','SELL'].includes(side))throw Error('slippage_inputs');
 const base=BigInt(capitalPolicy.slippage_base_bps_through_20cr);
 const scale=10000n;const bumped=side==='BUY'?scale+base:scale-base;
 if(bumped<=0n)throw Error('slippage_bounds');
 const out=div(ref*bumped,scale);if(out<=0n||out>9007199254740991n)throw Error('slippage_bounds');
 return {price_paise:Number(out),price:inrFromPaise(out),base_bps:Number(base)};
}
