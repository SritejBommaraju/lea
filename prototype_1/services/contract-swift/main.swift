// Contract test: the iOS app's own BridgeClient.swift + BridgeModels.swift, compiled for macOS and run
// against a live bridge. Every call decodes the bridge's real JSON into the app's types.
import CryptoKit
import Foundation

let env = ProcessInfo.processInfo.environment
let client = BridgeClient(baseURL: URL(string: env["LEA_URL"]!)!, token: env["LEA_TOKEN"]!)
let sid = env["LEA_SESSION"]!
var failures = 0
@MainActor func check(_ name: String, _ ok: Bool, _ detail: String = "") {
    print("\(ok ? "PASS" : "FAIL") \(name)\(detail.isEmpty ? "" : ": \(detail)")")
    if !ok { failures += 1 }
}

let health = try await client.health()
check("health decodes", health.spacetimedb.connected, "grain reachable=\(health.grain.reachable) agentReady=\(String(describing: health.grain.agentReady))")

let created = try await client.createSession(id: sid, title: "Swift contract")
check("create session", created.session.id == sid)

let file = FileManager.default.temporaryDirectory.appendingPathComponent("lea-contract-\(UUID()).jpg")
let bytes = Data("swift contract original \(UUID())".utf8)
try bytes.write(to: file)
let sha = SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined()
let asset = try await client.uploadAsset(sessionId: sid, assetId: "as_swift_closeup", kind: "photo_closeup", fileURL: file, mediaType: "image/jpeg", sha256: sha, meta: ["subject": "fan connector"])
check("upload original", asset.sha256 == sha && asset.bytes == bytes.count)
let again = try await client.uploadAsset(sessionId: sid, assetId: "as_swift_closeup", kind: "photo_closeup", fileURL: file, mediaType: "image/jpeg", sha256: sha)
check("repeat upload is idempotent", again.id == asset.id)
do {
    _ = try await client.uploadAsset(sessionId: sid, assetId: "as_swift_bad", kind: "photo_closeup", fileURL: file, mediaType: "image/jpeg", sha256: String(repeating: "0", count: 64))
    check("hash mismatch rejected", false)
} catch BridgeClient.Failure.http(let status, _) { check("hash mismatch rejected", status == 422, "HTTP \(status)") }
let back = try await client.assetData(sessionId: sid, assetId: asset.id)
check("original returned byte-for-byte", back == bytes)

let policy = try await client.freezePolicy(sessionId: sid, .init(scaleAnchorAxis: "longest", scaleAnchorMm: 80, holdoutAxis: "middle", holdoutMm: 46, maxHoldoutErrorMm: 1, minCoverage: 0.8, maxFitRmsMm: 1, minFitInlierShare: 0.9))
check("freeze policy", policy.scaleAnchorMm == 80)
do {
    _ = try await client.startReconstruction(sessionId: sid, .init(photoAssetIds: [asset.id], policyId: policy.id, referenceUnitsPerMm: 1, autoDesignObjective: nil, meshAssetId: nil, requestId: "swift-recon-1"))
    check("reconstruction gate", false)
} catch BridgeClient.Failure.http(let status, let message) { check("reconstruction refuses a non-orbit photo set", status == 409, message) }

let linked = try await client.linkGrain(sessionId: sid, projectId: nil, createName: "Lea Swift contract")
check("link real Grain project", linked.session.grainProjectId.count == 12, linked.session.grainProjectId)
let live = try await client.grainLive(sessionId: sid)
check("Grain live state decodes", live.linked && live.project?.id == linked.session.grainProjectId, "busy=\(live.project?.busy ?? false) files=\(live.files?.count ?? -1)")

let turn = try await client.recordVoiceTurn(sessionId: sid, id: "vt_swift_1", mode: "ptt", transcript: "Hey Leah, increase the mounting hole diameter by 2 mm")
check("voice turn routes an explicit design change", !turn.turn.commandId.isEmpty && turn.actions.isEmpty, turn.turn.replyText)
let unclear = try await client.recordVoiceTurn(sessionId: sid, id: "vt_swift_2", mode: "ptt", transcript: "make it better")
check("unclear request asks back, sends nothing", unclear.turn.commandId.isEmpty, unclear.turn.replyText)

do { _ = try await client.cartesiaGrant(); check("cartesia grant", true) }
catch BridgeClient.Failure.http(let status, let message) { check("cartesia reports unavailable (no key)", status == 503, message) }

let snap = try await client.snapshot(sid)
check("snapshot decodes every table", snap.designCommands.count == 1 && snap.voiceTurns.count == 2 && snap.assets.count == 1, "lastSeq=\(snap.lastSeq)")

var seen = 0
for try await event in client.events(sessionId: sid, after: 0) { seen += 1; if event.seq >= snap.lastSeq { break } }
check("event stream replays from seq 0", seen >= 5, "\(seen) events")

if let framePath = env["LEA_FRAME"] {
    let frame = try Data(contentsOf: URL(fileURLWithPath: framePath))
    let seen = try await client.look(sessionId: sid, frame: frame, subject: "fan connector", referenceAssetId: nil)
    check("live look decodes (stand-in Jev)", seen.part == "connector" && seen.box?.confidence == 0.65 && !seen.tip.isEmpty, seen.tip)
}

print(failures == 0 ? "ALL PASS" : "\(failures) FAILED")
exit(failures == 0 ? 0 : 1)
