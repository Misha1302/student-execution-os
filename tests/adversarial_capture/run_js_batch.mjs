import fs from 'node:fs';
import { parseTask, captureKind } from '../../src/student_execution_os/web/static/js/nlparse.js';

const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const now = new Date(input.now);
const out = input.cases.map((c) => {
  const parsed = parseTask(c.utterance, now);
  const effectiveKind = parsed.kind === 'EVENT' || parsed.kind === 'REMINDER'
    ? parsed.kind : captureKind(parsed, c.utterance);
  return { id: c.id, parsed, effective_kind: effectiveKind };
});
process.stdout.write(JSON.stringify(out));
