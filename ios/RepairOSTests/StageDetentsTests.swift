import Foundation
import Testing
@testable import RepairOS

/// The stage taking over while Lea answers (Report/Stage/StageDetents; the owner, 2026-10-25), and the name chip.
struct StageDetentsTests {
    @Test func anAnswerStartingGrowsTheStageOnce() {
        var d = StageDetents()
        #expect(d.detent == .report && d.sheetScrolls)
        let grew = d.answerStarted()
        #expect(grew)
        #expect(d.detent == .answer && d.answering && !d.sheetScrolls)
        // Her next sentences and moves change nothing.
        let grewAgain = d.answerStarted()
        #expect(!grewAgain)
        #expect(d.detent == .answer)
    }

    @Test func theAnswerEndingLeavesTheStageWhereItIs() {
        var d = StageDetents()
        d.answerStarted()
        d.answerEnded()
        #expect(d.detent == .answer)   // never yanked back up
        #expect(!d.answering)
        var e = StageDetents()
        e.answerStarted()
        e.expand()
        e.answerEnded()
        #expect(e.detent == .expanded)
    }

    @Test func pullingTheSheetUpDuringAnAnswerWins() {
        var d = StageDetents()
        d.answerStarted()
        d.userPulledSheetUp()
        #expect(d.detent == .report && d.sheetScrolls)
        #expect(!d.followsLea)
        // The rest of this answer leaves it alone.
        let grewDuring = d.answerStarted()
        #expect(!grewDuring)
        #expect(d.detent == .report)
        d.answerEnded()
        #expect(d.detent == .report)
        // The next answer grows the stage again, and follows Lea again.
        let grewNext = d.answerStarted()
        #expect(grewNext)
        #expect(d.detent == .answer && d.followsLea)
    }

    @Test func expandAndCollapse() {
        var d = StageDetents()
        d.expand()
        #expect(d.detent == .expanded && !d.followsLea)
        d.collapse()
        #expect(d.detent == .report)   // not answering: straight back to the report
        d.answerStarted()
        d.expand()
        d.collapse()
        #expect(d.detent == .answer)   // answering: one step back
        d.collapse()
        #expect(d.detent == .report)
    }

    @Test func anExpandedStageIsNotShrunkByANewAnswer() {
        var d = StageDetents()
        d.expand()
        let grewWhileExpanded = d.answerStarted()
        #expect(!grewWhileExpanded)
        #expect(d.detent == .expanded)
    }

    @Test func stageHeights() {
        #expect(StageDetents.stageHeight(.report, container: 800, touring: false) == 496)
        #expect(StageDetents.stageHeight(.answer, container: 800, touring: false) == 688)
        #expect(StageDetents.stageHeight(.expanded, container: 800, touring: false) == 800)
        #expect(StageDetents.stageHeight(.report, container: 800, touring: true) == 800)
        #expect(StageDetents.stageHeight(.report, container: 500, touring: false) == 380)   // never smaller than 380
        #expect(StageDetents.animation(reduceMotion: true) == nil)   // a cut
        #expect(StageDetents.animation(reduceMotion: false) != nil)
    }

    @Test func chipIsBrandAndModelOnly() {
        // The real identity line from the owner's Arduino scan, and the Xbox controller's.
        #expect(StageChip.text("Microcontroller development board · ELEGOO UNO R3") == "ELEGOO UNO R3")
        #expect(StageChip.text("Game controller · Xbox") == "Xbox")
        #expect(StageChip.text("Toaster oven") == "Toaster oven")   // no brand or model: the category
        #expect(StageChip.text("  ") == nil)
        #expect(StageChip.text(nil) == nil)
        #expect(StageChip.text("Board ·  ") == "Board")
    }

    // MARK: The photo card's corner

    /// A 393 x 690 stage's card area (below the top bar, above the caption strip and recentre control).
    static let area = CGRect(x: 16, y: 120, width: 361, height: 400)

    @Test func theCardGoesToTheCornerFarthestFromThePart() {
        // The part low on the right (where the old fixed card sat): the card moves to the top left.
        #expect(HandoffPlacement.pick(part: CGPoint(x: 330, y: 470), in: Self.area) == (.topLeading, HandoffPlacement.card))
        #expect(HandoffPlacement.pick(part: CGPoint(x: 40, y: 140), in: Self.area).corner == .bottomTrailing)
        #expect(HandoffPlacement.pick(part: CGPoint(x: 360, y: 130), in: Self.area).corner == .bottomLeading)
        // Unknown: the usual bottom trailing, full size.
        #expect(HandoffPlacement.pick(part: nil, in: Self.area) == (.bottomTrailing, HandoffPlacement.card))
    }

    @Test func theCardNeverCoversThePart() {
        for x in stride(from: 16.0, through: 377, by: 19) {
            for y in stride(from: 120.0, through: 520, by: 20) {
                let part = CGPoint(x: x, y: y)
                let p = HandoffPlacement.pick(part: part, in: Self.area)
                let card = HandoffPlacement.rect(p.corner, side: p.side, in: Self.area)
                #expect(!card.insetBy(dx: -8, dy: -8).contains(part), "card over the part at \(part)")
            }
        }
    }

    @Test func noClearCornerMeansASmallerCard() {
        // A cramped stage (a short screen with the caption strip up): at full size no corner keeps the clearance.
        let tight = CGRect(x: 16, y: 120, width: 300, height: 250)
        let p = HandoffPlacement.pick(part: CGPoint(x: 166, y: 245), in: tight)
        #expect(p.side == HandoffPlacement.smallCard)
    }
}
