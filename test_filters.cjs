// Exercise the generated report's actual script with a minimal DOM stand-in.
// This verifies filter logic, not native browser rendering or accessibility.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const page = fs.readFileSync(process.argv[2], 'utf8');
const script = page.match(/<script>([\s\S]*?)<\/script>/)[1];
const rows = [...page.matchAll(/<tr data-status="([^"]+)">([\s\S]*?)<\/tr>/g)]
  .map(m => ({dataset: {status: m[1]}, textContent: m[2].replace(/<[^>]*>/g, ' '), hidden: false}));
const controls = {
  status: {value: 'all', addEventListener() {}},
  query: {value: '', addEventListener() {}},
  count: {textContent: ''}, empty: {hidden: true},
};
const context = vm.createContext({document: {
  querySelectorAll: () => rows,
  getElementById: id => controls[id],
}});
vm.runInContext(script, context);
const visible = () => rows.filter(r => !r.hidden).length;
assert.equal(visible(), 6);
controls.status.value = 'ambiguous';
vm.runInContext('filterRows()', context);
assert.equal(visible(), 1);
controls.query.value = 'PAYMENT-103';
vm.runInContext('filterRows()', context);
assert.equal(visible(), 1);
controls.query.value = 'missing-reference';
vm.runInContext('filterRows()', context);
assert.equal(visible(), 0);
assert.equal(controls.empty.hidden, false);
controls.status.value = 'all';
controls.query.value = 'p2';
vm.runInContext('filterRows()', context);
assert.equal(visible(), 1);
controls.query.value = '';
vm.runInContext('filterRows()', context);
assert.equal(visible(), 6);
assert.equal(controls.count.textContent, '6 of 6 groups shown');
assert.equal(controls.empty.hidden, true);
console.log('6 filter scenarios passed (minimal DOM stand-in)');
