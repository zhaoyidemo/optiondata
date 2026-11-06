const assert = require('node:assert/strict');
const {test} = require('node:test');
const {calculate} = require('../static/stock-options-math.js');
const now = Date.UTC(2026,10,6,21);
const put = {type:'PUT',strike:125,bid:4.9,ask:5.45,bidSize:110,multiplier:100,expiresAt:now+7*86400000};
test('cash secured put uses full strike capital and contract-level fee',()=>{
  const r=calculate(put,130,.6,now);
  assert.equal(r.capital,12500);
  assert.ok(Math.abs(r.net-489.4)<1e-8);
  assert.ok(Math.abs(r.yield-.039152)<1e-8);
  assert.ok(Math.abs(r.apr-.039152*365/7)<1e-8);
  assert.ok(Math.abs(r.breakeven-120.106)<1e-8);
});
test('covered call separates premium income from assigned stock return',()=>{
  const r=calculate({...put,type:'CALL',strike:135,bid:4.45,ask:4.85},130,.6,now);
  assert.equal(r.capital,13000);
  assert.ok(Math.abs(r.assignedReturn-944.4/13000)<1e-8);
  assert.ok(Math.abs(r.breakeven-125.556)<1e-8);
});
test('deep ITM call premium does not hide stock loss on assignment',()=>{
  const r=calculate({...put,type:'CALL',strike:100,bid:29,ask:30},130,.6,now);
  assert.ok(r.yield>0);
  assert.ok(r.assignedReturn<0);
});
test('no fabricated yields for missing, crossed, adjusted or expired quotes',()=>{
  for(const overrides of [{bid:null},{bid:0},{bidSize:0},{ask:4},{multiplier:null},{expiresAt:now},{adjusted:true},{quoteMissing:true}]){
    const r=calculate({...put,...overrides},130,.6,now);
    assert.equal(r.eligible,false);assert.equal(r.apr,null);
  }
  assert.equal(calculate(put,130,NaN,now).eligible,false);
});
test('sub-day expiry suppresses annualization and fees can exceed premium',()=>{
  assert.equal(calculate({...put,expiresAt:now+3600000},130,.6,now).apr,null);
  assert.ok(calculate(put,130,500,now).yield<0);
});
