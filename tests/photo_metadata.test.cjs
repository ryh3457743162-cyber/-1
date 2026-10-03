const test = require('node:test');
const assert = require('node:assert/strict');
const metadata = require('../public/photo-metadata.js');
test('shooting year changes independently of archived/book year', () => {
  const p={year:'2026',shotDate:'2025-08-18'};
  assert.equal(metadata.year(p),'2025');assert.equal(p.year,'2026');
});
test('unknown dates use the existing archive year', () => {
  for(const shotDate of [null,undefined,''])assert.equal(metadata.year({year:'2026',shotDate}),'2026');
});
test('sort calendar dates without timezone conversion, preserving undated order', () => {
  const photos=[{id:'u1'}, {id:'late',shotDate:'2025-08-18'}, {id:'early',shotDate:'2025-08-16'}, {id:'u2'}];
  assert.deepEqual(metadata.sort(photos).map(p=>p.id),['early','late','u1','u2']);
  assert.equal(photos[0].id,'u1');
});
test('equal dates preserve original order', () => {
  const photos=[{id:1,shotDate:'2025-08-18'},{id:2,shotDate:'2025-08-18'}];
  assert.deepEqual(metadata.sort(photos),photos);
});
