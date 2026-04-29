"use strict";

(function () {
  var pollTimer = null;
  var busy = false;
  var pluginQuery = new URLSearchParams(window.location.search || "");
  var baseUrl = String(pluginQuery.get("base_url") || window.location.origin).replace(/\/+$/, "");
  var docKey = String(pluginQuery.get("doc_key") || "").trim();
  var targetType = String(pluginQuery.get("target_type") || "").trim();
  var targetId = String(pluginQuery.get("target_id") || "").trim();

  function endpoint(path) {
    return baseUrl + path;
  }

  function safeJsonParse(text) {
    if (!text) {
      return {};
    }
    try {
      return JSON.parse(text);
    } catch (_err) {
      return {};
    }
  }

  async function fetchJson(url, options) {
    var response = await fetch(url, options || {});
    var text = await response.text();
    var payload = safeJsonParse(text);
    if (!response.ok) {
      var detail = String(payload.error || payload.detail || text || ("HTTP " + response.status)).trim();
      throw new Error(detail);
    }
    return payload;
  }

  function normalizeOperations(rawOperations) {
    if (!Array.isArray(rawOperations)) {
      return [];
    }
    return rawOperations
      .filter(function (item) {
        return item && typeof item === "object" && item.op;
      })
      .slice(0, 50);
  }

  function executeOperations(operations) {
    return new Promise(function (resolve, reject) {
      window.Asc.scope.suScholarOperations = normalizeOperations(operations);

      window.Asc.plugin.callCommand(
        function () {
          var operationsList = Array.isArray(Asc.scope.suScholarOperations)
            ? Asc.scope.suScholarOperations.slice()
            : [];
          var doc = Api.GetDocument();
          var result = {
            applied: 0,
            requested: operationsList.length,
            errors: [],
          };

          if (doc && typeof doc.CreateNewHistoryPoint === "function") {
            doc.CreateNewHistoryPoint();
          }

          function normalizeText(text) {
            return String(text || "").replace(/\s+/g, " ").trim();
          }

          function operationIndex(op) {
            if (!op || typeof op !== "object") {
              return 0;
            }
            if (op.paragraph_index != null) {
              return Number(op.paragraph_index) || 0;
            }
            if (op.paragraph != null) {
              return Number(op.paragraph) || 0;
            }
            if (op.after_paragraph != null) {
              return Number(op.after_paragraph) || 0;
            }
            return 0;
          }

          function applyInsert(op, paragraphs) {
            var afterParagraph = Number(
              op.paragraph_index != null ? op.paragraph_index
              : op.after_paragraph != null ? op.after_paragraph
              : -1
            );
            var text = String(op.text || op.new_text || "").trim();

            if (!Number.isFinite(afterParagraph) || !text) {
              throw new Error("insert operation is invalid");
            }

            if (afterParagraph < 0 || afterParagraph >= paragraphs.length) {
              var endParagraph = Api.CreateParagraph();
              endParagraph.AddText(text);
              doc.Push(endParagraph);
              return true;
            }

            var target = paragraphs[afterParagraph];
            if (!target) {
              throw new Error("target paragraph was not found at index " + afterParagraph);
            }

            var insertedParagraph = Api.CreateParagraph();
            insertedParagraph.AddText(text);
            target.InsertParagraph(insertedParagraph, "after", false);
            return true;
          }


          function applyReplace(op, paragraphs) {
            var paragraphIndex = Number(op.paragraph_index != null ? op.paragraph_index : op.paragraph);
            var oldText = String(op.old_text || "");
            var newText = String(op.new_text || "");

            if (!Number.isFinite(paragraphIndex) || paragraphIndex < 0 || !oldText.trim()) {
              throw new Error("replace operation is invalid");
            }

            var paragraph = paragraphs[paragraphIndex];
            if (!paragraph) {
              throw new Error("target paragraph was not found at index " + paragraphIndex);
            }

            var currentText = String(paragraph.GetText() || "");
            var nextText = "";

            if (currentText.indexOf(oldText) !== -1) {
              nextText = currentText.replace(oldText, newText);
            } else {
              var normalizedCurrent = normalizeText(currentText);
              var normalizedOld = normalizeText(oldText);

              if (normalizedOld && normalizedCurrent.indexOf(normalizedOld) !== -1) {
                nextText = normalizedCurrent.replace(normalizedOld, newText);
              } else {
                var oldWords = normalizedOld.split(" ").filter(function (word) {
                  return word.length > 3;
                });
                var lowerCurrent = normalizedCurrent.toLowerCase();
                var matchCount = oldWords.filter(function (word) {
                  return lowerCurrent.indexOf(word.toLowerCase()) !== -1;
                }).length;

                if (oldWords.length > 0 && matchCount / oldWords.length >= 0.8) {
                  nextText = newText;
                } else {
                  throw new Error("old_text not found in paragraph " + paragraphIndex + ': "' + oldText.slice(0, 60) + '"');
                }
              }
            }

            paragraph.RemoveAllElements();
            if (nextText) {
              var run = Api.CreateRun();
              run.AddText(nextText);
              paragraph.AddElement(run);
            }
            return true;
          }


          function applyDeleteParagraph(op, paragraphs) {
            var paragraphIndex = Number(
              op.paragraph_index != null ? op.paragraph_index : op.paragraph
            );
            if (!Number.isFinite(paragraphIndex) || paragraphIndex < 0) {
              throw new Error("delete operation is invalid");
            }

            var paragraph = paragraphs[paragraphIndex];
            if (!paragraph) {
              throw new Error("target paragraph was not found at index " + paragraphIndex);
            }
            paragraph.Delete();
            return true;
          }

          var deleteOps = operationsList
            .filter(function (op) {
              var opType = String(op.op || "").trim().toLowerCase();
              return opType === "delete" || opType === "delete_paragraph";
            })
            .sort(function (left, right) {
              return operationIndex(right) - operationIndex(left);
            });
          var replaceOps = operationsList
            .filter(function (op) {
              return String(op.op || "").trim().toLowerCase() === "replace";
            })
            .sort(function (left, right) {
              return operationIndex(right) - operationIndex(left);
            });
          var insertOps = operationsList
            .filter(function (op) {
              var opType = String(op.op || "").trim().toLowerCase();
              return opType === "insert" || opType === "insert_after";
            })
            .sort(function (left, right) {
              return operationIndex(left) - operationIndex(right);
            });

          operationsList = deleteOps.concat(replaceOps).concat(insertOps);

          for (var index = 0; index < operationsList.length; index += 1) {
            var op = operationsList[index] || {};
            var opType = String(op.op || "").trim().toLowerCase();
            try {
              var paragraphs = doc.GetAllParagraphs();
              if (opType === "insert" || opType === "insert_after") {
                applyInsert(op, paragraphs);
              } else if (opType === "replace") {
                applyReplace(op, paragraphs);
              } else if (opType === "delete_paragraph" || opType === "delete") {
                applyDeleteParagraph(op, paragraphs);
              } else {
                throw new Error("unsupported operation: " + opType);
              }
              result.applied += 1;
            } catch (error) {
              result.errors.push({
                index: index,
                op: opType,
                message: error && error.message ? String(error.message) : "unknown operation error",
              });
            }
          }

          return JSON.stringify(result);
        },
        false,
        true,
        function (commandResult) {
          if (typeof commandResult === "string") {
            resolve(safeJsonParse(commandResult));
            return;
          }
          if (commandResult && typeof commandResult === "object") {
            resolve(commandResult);
            return;
          }
          resolve({ applied: 0, requested: 0, errors: [] });
        }
      );
    });
  }

  async function reportResult(commandId, payload) {
    if (!commandId) {
      return;
    }
    try {
      await fetchJson(endpoint("/plugins/llm-doc-editor/result"), {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json",
        },
        body: JSON.stringify({
          command_id: commandId,
          doc_key: docKey,
          target_type: targetType,
          target_id: targetId,
          result: payload,
        }),
      });
    } catch (error) {
      console.error("[su-scholar-plugin] result report failed", error);
    }
  }

  async function pollOnce() {
    if (busy || !docKey) {
      return;
    }
    busy = true;
    try {
      var nextPayload = await fetchJson(
        endpoint("/plugins/llm-doc-editor/next?doc_key=" + encodeURIComponent(docKey)),
        {
          method: "GET",
          headers: {
            Accept: "application/json",
          },
        }
      );

      if (!nextPayload || !nextPayload.command) {
        return;
      }

      var command = nextPayload.command;
      var commandId = String(command.id || "").trim();
      var operations = normalizeOperations(command.operations);

      if (!operations.length) {
        await reportResult(commandId, {
          status: "error",
          applied: 0,
          message: "Command does not contain operations.",
        });
        return;
      }

      var applyResult = await executeOperations(operations);
      var appliedCount = Number(applyResult && applyResult.applied ? applyResult.applied : 0);
      var errors = Array.isArray(applyResult && applyResult.errors) ? applyResult.errors : [];

      await reportResult(commandId, {
        status: errors.length ? "partial" : "applied",
        applied: appliedCount,
        requested: operations.length,
        errors: errors,
      });
    } catch (error) {
      console.error("[su-scholar-plugin] poll/apply failed", error);
    } finally {
      busy = false;
    }
  }

  function startPolling() {
    if (pollTimer || !docKey) {
      return;
    }
    pollOnce();
    pollTimer = window.setInterval(pollOnce, 1500);
  }

  window.Asc.plugin.init = function () {
    startPolling();
  };

  window.Asc.plugin.button = function () {};
})();
