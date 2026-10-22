// Runs the upstream scoring.ts (copied verbatim, see UPSTREAM_COMMIT) on JSON
// cases read from stdin. Used by tests/test_parity.py.
import { scoreSubmission } from './scoring.ts';
let input = '';
process.stdin.on('data', (c) => (input += c));
process.stdin.on('end', () => {
  const cases = JSON.parse(input);
  const out = cases.map((c) => scoreSubmission(c));
  process.stdout.write(JSON.stringify(out));
});
