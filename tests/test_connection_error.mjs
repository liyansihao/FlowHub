import test from 'node:test';
import assert from 'node:assert/strict';
import {connectionErrorDetails} from '../bridges/connection-error.mjs';
test('nested connection failures retain addresses without credential-bearing messages',()=>{
 const cause=Object.assign(new Error('secret URL and token'),{code:'UND_ERR_CONNECT_TIMEOUT',errors:[{code:'EHOSTUNREACH',address:'::1',port:443,syscall:'connect',headers:{token:'secret'}}]});
 const detail=connectionErrorDetails(new Error('fetch failed',{cause}));
 assert.equal(detail.cause.code,'UND_ERR_CONNECT_TIMEOUT');
 assert.equal(detail.cause.errors[0].address,'::1');
 assert.ok(!JSON.stringify(detail).includes('secret'));
 const cycle={code:'X'};cycle.cause=cycle;
 assert.ok(JSON.stringify(connectionErrorDetails(cycle)).length<200);
});
