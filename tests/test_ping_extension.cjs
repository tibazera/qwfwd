const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
let listener, messageListener, disconnectListener, sent;
const chrome = { runtime: { id:'test', onMessage:{addListener(fn){listener=fn;}},
  connectNative(name){ assert.equal(name,'com.qwfwd.ping'); return {
    onMessage:{addListener(fn){messageListener=fn;}}, onDisconnect:{addListener(fn){disconnectListener=fn;}},
    postMessage(message){sent=message;}
  };}
}};
vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../browser-extension/background.js'),'utf8'), {chrome,URL,setTimeout,clearTimeout});
const sender = {id:'test',frameId:0,url:'https://tibazera.github.io/qwfwd/'};
let result;
listener({op:'health'}, {...sender,url:'https://evil.example/'}, r=>result=r);
assert.equal(result.error,'origin_denied');
assert.equal(listener({op:'health'},sender,r=>result=r),true);
messageListener({id:sent.id,ok:true}); assert(result.ok);
listener({op:'ping',target:'1.1.1.1:27500'},sender,r=>result=r);
  chrome.runtime.lastError = {message:'Specified native messaging host not found.'};
  disconnectListener(); assert.equal(result.error,'helper_unavailable');
  assert.equal(result.detail,'Specified native messaging host not found.');
console.log('PASS: origin restriction, request/reply correlation, helper disconnection');
