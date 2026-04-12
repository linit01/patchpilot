import SwiftUI

/// Global patch history timeline
struct PatchHistoryView: View {
    @StateObject private var patchService = PatchService()
    @State private var selectedRecord: PatchHistoryRecord?
    @State private var errorMessage: String?

    var body: some View {
        NavigationStack {
            List {
                if patchService.isLoading && patchService.history.isEmpty {
                    ProgressView()
                        .frame(maxWidth: .infinity)
                        .listRowBackground(Theme.bgBlack)
                } else if patchService.history.isEmpty {
                    Text("No patch history yet")
                        .foregroundColor(Theme.textMuted)
                        .listRowBackground(Theme.bgBlack)
                } else {
                    ForEach(patchService.history) { record in
                        Button {
                            selectedRecord = record
                        } label: {
                            historyRow(record)
                        }
                        .listRowBackground(Theme.bgPanel)
                    }
                }
            }
            .listStyle(.plain)
            .scrollContentBackground(.hidden)
            .background(Theme.bgBlack)
            .navigationTitle("Patch History")
            .refreshable {
                do {
                    try await patchService.fetchHistory()
                } catch {
                    errorMessage = error.localizedDescription
                }
            }
            .task {
                do {
                    try await patchService.fetchHistory()
                } catch {
                    errorMessage = error.localizedDescription
                }
            }
            .sheet(item: $selectedRecord) { record in
                PatchDetailSheet(record: record)
            }
        }
    }

    private func historyRow(_ record: PatchHistoryRecord) -> some View {
        HStack(spacing: 12) {
            Image(systemName: record.isSuccess ? "checkmark.circle.fill" : "xmark.circle.fill")
                .font(.title3)
                .foregroundColor(record.isSuccess ? Theme.green : Theme.red)

            VStack(alignment: .leading, spacing: 4) {
                Text(record.hostname ?? "Unknown Host")
                    .font(.subheadline)
                    .fontWeight(.medium)
                    .foregroundColor(Theme.textPrimary)

                Text(record.createdAt ?? "—")
                    .font(.caption)
                    .foregroundColor(Theme.textSecondary)
            }

            Spacer()

            VStack(alignment: .trailing, spacing: 4) {
                if let pkgs = record.packagesUpdated, pkgs > 0 {
                    Text("\(pkgs) pkg\(pkgs == 1 ? "" : "s")")
                        .font(.caption)
                        .foregroundColor(Theme.cyan)
                }
                Text(record.durationDisplay)
                    .font(.caption2)
                    .foregroundColor(Theme.textMuted)
            }
        }
        .padding(.vertical, 4)
    }
}

extension PatchHistoryRecord: Hashable {
    static func == (lhs: PatchHistoryRecord, rhs: PatchHistoryRecord) -> Bool { lhs.id == rhs.id }
    func hash(into hasher: inout Hasher) { hasher.combine(id) }
}

/// Detail sheet showing full patch output
struct PatchDetailSheet: View {
    let record: PatchHistoryRecord

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    // Summary
                    HStack {
                        Image(systemName: record.isSuccess ? "checkmark.circle.fill" : "xmark.circle.fill")
                            .foregroundColor(record.isSuccess ? Theme.green : Theme.red)
                        Text(record.isSuccess ? "Success" : "Failed")
                            .fontWeight(.semibold)
                            .foregroundColor(record.isSuccess ? Theme.green : Theme.red)
                        Spacer()
                        Text(record.durationDisplay)
                            .foregroundColor(Theme.textMuted)
                    }

                    if let error = record.errorMessage, !error.isEmpty {
                        Text(error)
                            .font(.subheadline)
                            .foregroundColor(Theme.red)
                            .padding(8)
                            .background(Theme.red.opacity(0.1))
                            .cornerRadius(6)
                    }

                    // Full output
                    if let output = record.output, !output.isEmpty {
                        Text("Output")
                            .font(.headline)
                            .foregroundColor(Theme.textPrimary)

                        Text(output)
                            .font(.system(.caption, design: .monospaced))
                            .foregroundColor(Theme.textSecondary)
                            .padding(8)
                            .background(Theme.bgCardInner)
                            .cornerRadius(6)
                    }
                }
                .padding()
            }
            .background(Theme.bgBlack)
            .navigationTitle(record.hostname ?? "Patch Detail")
            .navigationBarTitleDisplayMode(.inline)
        }
        .presentationDetents([.medium, .large])
    }
}
