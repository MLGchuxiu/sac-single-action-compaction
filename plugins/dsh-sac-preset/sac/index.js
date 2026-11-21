/**
 * Entry point for the `sac` preset's compaction policy.
 *
 * The Single-Action Compaction implementation lives in the sibling plugin
 * package (`plugins/dsh-sac-compact`); this subpath re-exports it so the
 * preset bundle stays self-contained without duplicating the source.
 *
 * @module dsh-sac-preset/sac
 */
export * from '../../dsh-sac-compact/index.js'
export { default } from '../../dsh-sac-compact/index.js'