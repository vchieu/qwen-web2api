const fs = require('fs');
const vm = require('vm');

let code = fs.readFileSync('extension/bridge.js', 'utf8');
code = code.replace('loop();', '// loop();');
code = code.replace('async function handleData(', 'globalThis.handleData = async function handleData(');
code = code.replace('async function send(jobId, ev) {', 'async function send(jobId, ev) { if (globalThis.onSend) return globalThis.onSend(jobId, ev);');
code = 'function __runBridge() {\n' + code + '\n}\n__runBridge();';

const events = [];

function FakeXHR() {}
FakeXHR.prototype = { open: () => {}, send: () => {} };

const sandbox = {
  window: {},
  document: { querySelectorAll: () => [], activeElement: null },
  location: { origin: 'https://chat.qwen.ai', host: 'chat.qwen.ai', search: '' },
  sessionStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
  XMLHttpRequest: FakeXHR,
  fetch: async () => ({ ok: true, json: async () => ({}) }),
  setTimeout: () => 1,
  clearTimeout: () => {},
  setInterval: () => 1,
  console: { log: () => {} },
  onSend: async (id, ev) => { events.push(ev); }
};

vm.createContext(sandbox);
vm.runInContext(code, sandbox);

async function run() {
  const job = { id: 'test_job', toolsActive: false };

  console.log('--- TEST 1: Disjoint chunks (like mock test) ---');
  const payload1 = JSON.stringify({
    choices: [{
      delta: {
        role: 'assistant',
        content: '',
        phase: 'thinking_summary',
        extra: {
          summary_title: { content: ['Analyzing the uploaded document to determine the required task'] },
          summary_thought: { content: ['I am reviewing the contents of the provided text file to understand the user\'s specific request.'] }
        },
        status: 'typing'
      }
    }]
  });

  const payloadFinish1 = JSON.stringify({
    choices: [{ delta: { role: 'assistant', content: '', phase: 'thinking_summary', status: 'finished' } }]
  });

  const payload2 = JSON.stringify({
    choices: [{
      delta: {
        role: 'assistant',
        content: '',
        phase: 'thinking_summary',
        extra: {
          summary_title: { content: ['Verifying the availability of the file reading capability'] },
          summary_thought: { content: ['I am double-checking the list of accessible functions.'] }
        },
        status: 'typing'
      }
    }]
  });

  await sandbox.handleData(job, payload1);
  await sandbox.handleData(job, payloadFinish1);
  await sandbox.handleData(job, payload2);

  events.forEach((e, i) => {
    console.log(`[Event ${i+1}] kind=${e.kind} phase=${e.phase}: ${JSON.stringify(e.text)}`);
  });

  console.log('\n--- TEST 2: Real Qwen cumulative chunks (as reported by user) ---');
  events.length = 0;
  const realJob = { id: 'real_job', toolsActive: false };

  const qwenChunk1 = JSON.stringify({
    choices: [{
      delta: {
        role: 'assistant', content: '', phase: 'thinking_summary',
        extra: {
          summary_title: { content: ['Analyzing the uploaded project structure'] },
          summary_thought: { content: ['I am reviewing the provided source files.'] }
        },
        status: 'typing'
      }
    }]
  });

  const qwenChunk2 = JSON.stringify({
    choices: [{
      delta: {
        role: 'assistant', content: '', phase: 'thinking_summary',
        extra: {
          summary_title: { content: ['Analyzing the uploaded project structure', 'Analyzing the codebase for vulnerabilities'] },
          summary_thought: { content: ['I am reviewing the provided source files.', 'I am examining the Python modules.'] }
        },
        status: 'typing'
      }
    }]
  });

  const qwenChunk3 = JSON.stringify({
    choices: [{
      delta: {
        role: 'assistant', content: '', phase: 'thinking_summary',
        extra: {
          summary_title: { content: ['Analyzing the uploaded project structure', 'Analyzing the codebase for vulnerabilities', 'Analyzing the request handling logic'] },
          summary_thought: { content: ['I am reviewing the provided source files.', 'I am examining the Python modules.', 'I evaluate potential vulnerabilities.'] }
        },
        status: 'typing'
      }
    }]
  });

  await sandbox.handleData(realJob, qwenChunk1);
  await sandbox.handleData(realJob, qwenChunk2);
  await sandbox.handleData(realJob, qwenChunk3);

  events.forEach((e, i) => {
    console.log(`[Real Event ${i+1}] kind=${e.kind} phase=${e.phase}: ${JSON.stringify(e.text)}`);
  });
}

run();
