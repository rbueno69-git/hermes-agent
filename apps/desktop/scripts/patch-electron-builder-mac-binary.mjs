import fs from 'node:fs'
import path from 'node:path'

const desktopRoot = path.resolve(import.meta.dirname, '..')
const repoRoot = path.resolve(desktopRoot, '..', '..')
const installedElectronMacPath = path.join(repoRoot, 'node_modules', 'app-builder-lib', 'out', 'electron', 'electronMac.js')
const marker = 'hermes-macos-electron-binary-fallback'
const renameNeedle = '    await doRename(path.join(contentsPath, "MacOS"), electronBranding.productName, appPlist.CFBundleExecutable);'
const replacement = `    // ${marker}: restore a missing main executable before electron-builder renames it.
    const macosDir = path.join(contentsPath, "MacOS");
    const bundledElectronBinary = path.join(macosDir, electronBranding.productName);
    if (!fs.existsSync(bundledElectronBinary)) {
        const candidates = [
            path.join(packager.info.framework.distMacOsAppName, "Contents", "MacOS", electronBranding.productName),
            path.join(process.cwd(), "node_modules", "electron", "dist", "Electron.app", "Contents", "MacOS", electronBranding.productName),
            path.join(process.cwd(), "..", "..", "node_modules", "electron", "dist", "Electron.app", "Contents", "MacOS", electronBranding.productName),
        ];
        const sourceBinary = candidates.find(candidate => fs.existsSync(candidate));
        if (sourceBinary == null) {
            throw new Error("Electron binary missing from packaged app and Electron runtime: " + bundledElectronBinary);
        }
        await (0, promises_1.copyFile)(sourceBinary, bundledElectronBinary);
        await (0, promises_1.chmod)(bundledElectronBinary, 0o755);
    }
    await doRename(macosDir, electronBranding.productName, appPlist.CFBundleExecutable);`

function loadCompatibleSource(electronMacPath) {
  if (!fs.existsSync(electronMacPath)) {
    throw new Error(`[patch-electron-builder] required file not found: ${electronMacPath}`)
  }
  const source = fs.readFileSync(electronMacPath, 'utf8')
  const patchedShape = [
    marker,
    'const macosDir = path.join(contentsPath, "MacOS");',
    'await (0, promises_1.copyFile)(sourceBinary, bundledElectronBinary);',
    'await doRename(macosDir, electronBranding.productName, appPlist.CFBundleExecutable);',
  ]
  const isPatched = patchedShape.every(part => source.includes(part))
  if (!isPatched && !source.includes(renameNeedle)) {
    throw new Error('[patch-electron-builder] incompatible app-builder-lib electronMac.js shape')
  }
  return source
}

function applySafeguard(electronMacPath) {
  const source = loadCompatibleSource(electronMacPath)
  if (source.includes(marker)) {
    console.log('[patch-electron-builder] macOS Electron binary fallback already applied')
    return
  }
  fs.writeFileSync(electronMacPath, source.replace(renameNeedle, replacement))
  loadCompatibleSource(electronMacPath)
  console.log('[patch-electron-builder] applied macOS Electron binary fallback')
}

const checkIndex = process.argv.indexOf('--check')
if (checkIndex !== -1) {
  loadCompatibleSource(process.argv[checkIndex + 1] ? path.resolve(process.argv[checkIndex + 1]) : installedElectronMacPath)
  console.log('[patch-electron-builder] safeguard compatible')
  process.exit(0)
}

const applyIndex = process.argv.indexOf('--apply')
if (applyIndex !== -1) {
  applySafeguard(process.argv[applyIndex + 1] ? path.resolve(process.argv[applyIndex + 1]) : installedElectronMacPath)
  process.exit(0)
}

if (process.platform === 'darwin') applySafeguard(installedElectronMacPath)
