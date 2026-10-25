import SwiftUI
import UIKit

/// How much of the screen the 3D stage takes on the report (the owner, 2026-10-25: "when Lea answers a question … the
/// animation of where things are on the model should be bigger"). Kept apart from the view so every rule is tested:
///
/// - `report`: the stage over the report sheet (about 62% of the screen); the sheet scrolls as usual.
/// - `answer`: Lea is answering: the sheet drops to a peek and the stage takes most of the screen.
/// - `expanded`: a swipe up on the caption strip (or a tap on it): the stage fills the screen, the model and the
///   caption with a thin transcript over it.
///
/// An answer starting moves the stage from `report` to `answer`, once per answer. Whatever the person does with it
/// (pulling the sheet up, expanding, collapsing) wins for the rest of that answer, and an answer ending never moves
/// anything: the stage stays where they left it.
struct StageDetents: Equatable, Sendable {
    enum Detent: Equatable, Sendable { case report, answer, expanded }

    private(set) var detent: Detent = .report
    /// Lea is answering now (from her first sentence or first move until the answer ends).
    private(set) var answering = false
    /// The person moved the stage or the sheet during this answer: Lea's later moves leave the layout alone.
    private(set) var userMoved = false

    init(detent: Detent = .report) { self.detent = detent }

    /// Her first sentence or first move of an answer. True when the stage grew.
    @discardableResult
    mutating func answerStarted() -> Bool {
        guard !answering else { return false }
        answering = true
        userMoved = false
        guard detent == .report else { return false }
        detent = .answer
        return true
    }

    /// The answer finished (or was stopped): the layout stays as it is, never yanked back.
    mutating func answerEnded() { answering = false }

    /// The person pulled the sheet up: the report comes back and stays for the rest of this answer.
    mutating func userPulledSheetUp() {
        userMoved = true
        detent = .report
    }

    /// A swipe up on the caption strip, or a tap on it: the stage fills the screen.
    mutating func expand() {
        userMoved = true
        detent = .expanded
    }

    /// A swipe down, or "Report": one step back towards the report.
    mutating func collapse() {
        userMoved = true
        detent = detent == .expanded ? (answering ? .answer : .report) : .report
    }

    /// The report sheet scrolls only in `report`; above that the stage owns the screen (the sheet peeks below it).
    var sheetScrolls: Bool { detent == .report }
    /// Lea's moves bring the stage back into view only while the person has not moved it themselves.
    var followsLea: Bool { !userMoved }

    /// The stage's height in a container `height` tall (the walkthrough takes the whole screen too).
    static func stageHeight(_ detent: Detent, container height: CGFloat, touring: Bool) -> CGFloat {
        if touring || detent == .expanded { return height }
        let share: CGFloat = detent == .answer ? 0.86 : 0.62
        return max(380, height * share)
    }

    /// A ~0.45 s critically damped spring; a cut under Reduce Motion.
    static func animation(reduceMotion: Bool) -> Animation? {
        reduceMotion ? nil : .spring(response: 0.45, dampingFraction: 1)
    }
}

/// The object's name on the stage as a compact chip: the brand and model only ("ELEGOO UNO R3"); the category
/// ("Microcontroller development board") is left to the accessibility label. With no brand or model, the category.
enum StageChip {
    static func text(_ identity: String?) -> String? {
        guard let identity = identity?.trimmedOrNil else { return nil }
        let parts = identity.split(separator: "·").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
        return parts.count > 1 ? parts.dropFirst().joined(separator: " ") : parts.first
    }
}

/// The window's top safe-area inset (status bar, Dynamic Island), for laying the stage out under the report's own
/// top bar: the stage ignores the top safe area, so it can't read it from its own geometry.
@MainActor
enum StageMetrics {
    static var windowTopInset: CGFloat {
        let scenes = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }
        let window = scenes.flatMap(\.windows).first { $0.isKeyWindow } ?? scenes.first?.windows.first
        return window?.safeAreaInsets.top ?? 48
    }
}

/// Where the proof photo card goes on the stage: the corner farthest from the part Lea is showing, so the card never
/// sits on the part, its ring or its label (the owner, 2026-10-25). A corner is clear when the card stays `clearance`
/// points from the part (room for the ring and the label out along the surface). No clear corner: a smaller card.
enum HandoffPlacement {
    enum Corner: CaseIterable, Equatable, Sendable { case topLeading, topTrailing, bottomLeading, bottomTrailing }

    static let card: CGFloat = 120
    static let smallCard: CGFloat = 84
    static let clearance: CGFloat = 56

    /// The card's frame for `corner` inside `area` (the stage minus its top bar and bottom controls).
    static func rect(_ corner: Corner, side: CGFloat, in area: CGRect) -> CGRect {
        let x = corner == .topLeading || corner == .bottomLeading ? area.minX : area.maxX - side
        let y = corner == .topLeading || corner == .topTrailing ? area.minY : area.maxY - side
        return CGRect(x: x, y: y, width: side, height: side)
    }

    /// The corner and card size for a part at `part` (stage points; nil: not known, the usual bottom trailing).
    static func pick(part: CGPoint?, in area: CGRect) -> (corner: Corner, side: CGFloat) {
        guard let part else { return (.bottomTrailing, card) }
        func distance(_ r: CGRect) -> CGFloat {
            let dx = max(r.minX - part.x, 0, part.x - r.maxX), dy = max(r.minY - part.y, 0, part.y - r.maxY)
            return hypot(dx, dy)
        }
        for side in [card, smallCard] {
            let clear = Corner.allCases.map { ($0, distance(rect($0, side: side, in: area))) }.filter { $0.1 >= clearance }
            // Among the clear corners, the farthest from the part; ties keep the usual bottom trailing first.
            if let best = clear.max(by: { $0.1 < $1.1 || ($0.1 == $1.1 && $0.0 == .bottomTrailing) }) { return (best.0, side) }
        }
        let far = Corner.allCases.max { distance(rect($0, side: smallCard, in: area)) < distance(rect($1, side: smallCard, in: area)) }!
        return (far, smallCard)
    }
}
