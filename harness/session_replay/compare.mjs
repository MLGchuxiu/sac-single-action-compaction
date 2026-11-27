// Before/after comparison on a logged session, as JSON for the report.
//   node compare.mjs <session.v4.jsonl>      (prints JSON: before/after on one log)
import { resolve } from 'node:path'
import { writeFileSync } from 'node:fs'
import { loadLog, kindOf, textOf } from './replay.mjs'
import { simulate } from './simulate.mjs'

const file = resolve(process.argv[2])
const log = loadLog(file)
const oldRun = await simulate(file, 'old')
const newRun = await simulate(file, 'new')
const turnStarts = log.order.filter((seq) => log.events.get(seq).type === 'step/start' && log.events.get(seq).data.step === 1)
const foldAt = (run, seq) => run.folds.find((fold) => fold.at === seq)
const summary = {
  log: file.split('/').slice(-3).join('/'),
  folds: {
    old: oldRun.folds.length,
    new: newRun.folds.length,
    newByKind: newRun.folds.reduce((acc, fold) => ({ ...acc, [fold.kind]: (acc[fold.kind] ?? 0) + 1 }), {}),
    oldFoldsThatFoldedAwayARequest: oldRun.folds.filter((fold) => fold.foldedRequests.length > 0).length,
    newFoldsThatFoldedAwayARequest: newRun.folds.filter((fold) => fold.foldedRequests.length > 0).length,
    newOfWhichArchiveFoldsQuotingItVerbatim: newRun.folds.filter((fold) => fold.kind === 'archive' && fold.foldedRequests.length > 0).length,
  },
  tokens: {
    oldMean: Math.round(oldRun.meanTokens), newMean: Math.round(newRun.meanTokens),
    oldPeak: oldRun.maxTokens, newPeak: newRun.maxTokens,
    oldFinal: oldRun.finalTokens, newFinal: newRun.finalTokens,
  },
  instructions: newRun.instructions.map((item, index) => {
    const before = oldRun.instructions[index]
    return {
      seq: item.seq,
      kind: item.kind,
      text: item.text.slice(0, 60).replace(/\n/g, '⏎'),
      repliesAfterIt: item.totalRepliesAfter,
      oldSawRaw: before.repliesSeenRaw,
      newSawRaw: item.repliesSeenRaw,
      newSawRawOrQuoted: item.repliesSeenRawOrQuoted,
      newPinnedAtEnd: item.stillOnSurfaceAtEnd,
      newQuotedInArchive: item.carriedAtEnd,
    }
  }),
  turnBoundaries: turnStarts.map((seq) => ({
    seq,
    turn: log.events.get(seq).data.turn,
    oldFold: foldAt(oldRun, seq)?.kind ?? null,
    newFold: foldAt(newRun, seq)?.kind ?? null,
  })),
  finalSurface: newRun.finalSurface,
}
console.log(JSON.stringify(summary, null, 2))
