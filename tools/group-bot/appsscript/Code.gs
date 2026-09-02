/**
 * 總務處 LINE 群組機器人 —— Google Apps Script 版。
 *
 * 取代原本「uvicorn + opencode serve + ngrok」本機堆疊(見
 * production-worktrees/group-bot/tools/group-bot/app/)。行為對照:
 *   - app/api/line_webhook.py      -> doPost / processEvent_ / handleGroupMessage_
 *   - app/services/opencode_agent.py -> buildPrompt_ / shouldSuppress_ / callGeminiChain_
 *   - app/services/conversation.py -> loadState_ / saveState_ / addMessage_ 等
 *   - app/services/line_client.py  -> sendText_ / lineReply_ / linePush_
 *   - app/services/allowlist.py    -> isGroupAllowed_
 *
 * 私訊(DM)刻意改成開放任何人、一律視為直接對話(force reply),沿用跟
 * 群組一樣的人設/知識庫回答——不含本機 admin 版本(讀寫檔案/跑指令)的
 * 工具能力,那個版本保留在本機 opencode admin agent,未來要切回去仍然
 * 可用。唯一例外是指令碼屬性 OWNER_USER_ID 指定的本人:私訊本人時改用
 * Prompt.gs 的 OWNER_SYSTEM_PROMPT(不受總務處業務範圍限制的通用助理),
 * 其他人私訊維持總務處人設。
 *
 * 需要在「專案設定 -> 指令碼屬性」設定:
 *   LINE_CHANNEL_ACCESS_TOKEN, LINE_ALLOWED_GROUPS, GEMINI_API_KEY, WEBHOOK_KEY,
 *   OWNER_USER_ID(選填,私訊本人開放任何話題用)
 */

// --- 設定 ---------------------------------------------------------------

// Gemini 模型 fallback 鏈:第一棒失敗/被擋就換下一棒。比照原本 Python 版
// opencode 多棒 fallback 的精神,但改成只打 Gemini 官方 API,不再需要
// opencode/多 provider 的複雜度。2026-08-27 實測 gemini-2.5-flash/-lite
// 已下架(404 "no longer available to new users")。使用者要求「用最
// 優惠的」,改成 gemini-3.5-flash-lite 當主力(定價 $0.30/$2.50,每
// 百萬 token,是當時可用型號裡最便宜的),失敗/限流才退到
// gemini-3.7-flash(較貴但較強)當備援,不是完全不設備援以免主力被
// 限流時整個安靜下來。這兩個型號之後可能同樣過期,發現打不通時上
// AI Studio 選型頁(aistudio.google.com,右上角模型選單)確認目前
// 可用型號與定價,直接改這個陣列就好,不用改其他程式碼。
const MODEL_CHAIN = ['gemini-3.5-flash-lite', 'gemini-3.6-flash', 'gemini-3.7-flash', 'gemini-3.5-flash'];

// 每個群組/私訊保留的對話輪數(含機器人自己的回覆),對應原本
// GROUPBOT_CONTEXT_WINDOW。CacheService 最長只能存 6 小時,比 Python
// 版「行程重啟就清空」更持久一點,對問答機器人來說是可接受的差異。
const CONTEXT_WINDOW = 10;
const CACHE_TTL_SECONDS = 6 * 60 * 60; // CacheService 上限
const BOT_LABEL = '機器人';
const BOT_MESSAGE_ID_HISTORY = 50;

// 使用者反映固定寫死的單一 fallback 句子讀起來很制式、每次都一樣。
// 改成隨機挑一句、語氣稍微輕鬆一點的多種說法。
const MENTION_FALLBACK_REPLIES = [
  '你好,這則訊息看起來跟總務處業務比較沒關係,不過如果有採購、修繕、場地、動植物之類的事,隨時都可以再跟我說喔。',
  '這題好像不太算我平常顧的總務範圍耶,但真的有修繕、採購、場地借用這類需求,儘管來找我。',
  '欸這個不太是我的守備範圍(總務業務啦),不過修繕、採購、場地這類的事,我隨時待命。',
  '嗯這跟總務業務比較沒關係,怕亂回答誤導你,總務相關的事情倒是可以放心問我。',
  '哈哈這個我可能幫不上忙(不算總務業務範圍),但修繕、採購、動植物這類的事找我準沒錯。',
];

function pickFallbackReply_() {
  return MENTION_FALLBACK_REPLIES[Math.floor(Math.random() * MENTION_FALLBACK_REPLIES.length)];
}

const ALL_MODELS_FAILED_REPLY = '抱歉,目前系統暫時忙碌,請稍後再試一次。';

const LINE_API_BASE = 'https://api.line.me/v2/bot';
const MAX_TEXT_LEN = 5000;

const SILENT_RE = /^[\s"'「」『』(（]*\(silent\)[\s"'「」『』)）.,。，!！]*$/i;

// --- 入口 -----------------------------------------------------------------

function doPost(e) {
  try {
    const expectedKey = getProp_('WEBHOOK_KEY');
    const givenKey = (e && e.parameter && e.parameter.key) || '';
    if (!expectedKey || givenKey !== expectedKey) {
      // 拿不到 LINE 的簽章 header(Apps Script 平台限制),改用網址上的
      // 亂數 key 擋掉隨便亂打的請求。key 不符就直接不處理,但仍回
      // 200,避免暴露驗證細節。
      return ContentService.createTextOutput('ok');
    }

    const body = e && e.postData && e.postData.contents;
    if (!body) return ContentService.createTextOutput('ok');

    const payload = JSON.parse(body);
    const events = payload.events || [];
    for (const event of events) {
      try {
        processEvent_(event);
      } catch (err) {
        Logger.log('processEvent_ error: ' + err + '\n' + (err && err.stack));
      }
    }
  } catch (err) {
    Logger.log('doPost error: ' + err + '\n' + (err && err.stack));
  }
  return ContentService.createTextOutput('ok');
}

function doGet(e) {
  return ContentService.createTextOutput('ok');
}

// --- 事件分流(對應 app/api/line_webhook.py) --------------------------

function processEvent_(event) {
  const sourceType = (event.source || {}).type;
  if (event.type !== 'message' || (sourceType !== 'group' && sourceType !== 'user')) {
    return;
  }
  const message = event.message || {};
  if (message.type !== 'text') return;

  const replyToken = event.replyToken || '';
  const text = message.text || '';
  if (!replyToken) return;

  if (sourceType === 'group') {
    handleGroupMessage_(event.source, message, replyToken, text);
  } else {
    handleDmMessage_(event.source, replyToken, text);
  }
}

function handleGroupMessage_(source, message, replyToken, text) {
  const groupId = source.groupId || '';
  const userId = source.userId || '';
  if (!groupId) return;
  if (!isGroupAllowed_(groupId)) {
    // 故意印完整 groupId(不截斷)——這是使用者取得新群組 ID、貼進
    // LINE_ALLOWED_GROUPS 白名單唯一的辦法:LINE 沒有「列出機器人在哪些
    // 群組」的 API,只能先把機器人加進群組、讓人發一則訊息,再從這裡的
    // 執行紀錄複製完整 ID。
    Logger.log('Ignored message from non-allowlisted group ' + groupId);
    // 執行項目頁面的細節面板在這台機器上一直打不開(自動化測試多次失敗),
    // 額外把 groupId 存進 CacheService,讓 debugRecentUnlistedGroups()
    // 用「選函式 -> 執行」這個確定可靠的路徑印出來,不用依賴那個面板。
    rememberUnlistedGroup_(groupId);
    return;
  }

  const mentionees = ((message.mention || {}).mentionees) || [];
  if (mentionees.some(m => m.type === 'all')) {
    Logger.log('Ignored @all broadcast in group ' + groupId.slice(0, 8) + '...');
    return;
  }
  const wasMentioned = mentionees.some(m => m.isSelf);
  const quotedMessageId = message.quotedMessageId;

  const convKey = 'group_' + groupId;
  const state = loadState_(convKey);
  const isReplyToBot = !!quotedMessageId && state.botMessageIds.indexOf(quotedMessageId) !== -1;
  const forceReply = wasMentioned || isReplyToBot;

  const label = labelForUser_(state, userId);
  addMessage_(state, label, text);
  const historyForPrompt = state.history.slice(0, -1);
  const prompt = buildPrompt_(historyForPrompt, label, text, wasMentioned, isReplyToBot);

  const result = callGeminiChain_(prompt);
  if (!result) {
    if (forceReply) {
      Logger.log('group=' + groupId + ' directly addressed but all models failed');
    }
    saveState_(convKey, state);
    return;
  }

  let answer = result.text;
  if (shouldSuppress_(answer)) {
    if (!forceReply) {
      saveState_(convKey, state);
      return;
    }
    answer = pickFallbackReply_();
  }

  const displayName = getGroupMemberDisplayName_(state, groupId, userId);
  const finalAnswer = displayName ? displayName + '老師好,' + answer : answer;

  addMessage_(state, BOT_LABEL, finalAnswer);
  const sentIds = sendText_(replyToken, groupId, finalAnswer);
  sentIds.forEach(id => rememberBotMessageId_(state, id));
  saveState_(convKey, state);
}

function handleDmMessage_(source, replyToken, text) {
  const userId = source.userId || '';
  if (!userId) return;

  const ownerUserId = getProp_('OWNER_USER_ID');
  const isOwner = !!ownerUserId && userId === ownerUserId;

  const convKey = 'dm_' + userId;
  const state = loadState_(convKey);
  const label = labelForUser_(state, userId);
  addMessage_(state, label, text);
  const historyForPrompt = state.history.slice(0, -1);
  // 私訊一律視為直接對話(force reply),比照群組被 @ 到的行為——見
  // production plan 的「DM 一律視為直接對話」決策。
  const prompt = buildPrompt_(historyForPrompt, label, text, true, false);

  // 本人私訊用不受總務處業務範圍限制的通用助理人設(見 Prompt.gs 的
  // OWNER_SYSTEM_PROMPT);其他人私訊維持總務處人設,跟群組一樣。
  const result = callGeminiChain_(prompt, isOwner ? OWNER_SYSTEM_PROMPT : undefined);
  let answer;
  if (!result) {
    answer = ALL_MODELS_FAILED_REPLY;
  } else if (isOwner) {
    answer = result.text;
  } else {
    answer = shouldSuppress_(result.text) ? pickFallbackReply_() : result.text;
  }

  // 本人不用「XX老師好」開場問候,通用助理模式下比較自然。
  let finalAnswer = answer;
  if (!isOwner) {
    const displayName = getUserProfileDisplayName_(state, userId);
    finalAnswer = displayName ? displayName + '老師好,' + answer : answer;
  }

  addMessage_(state, BOT_LABEL, finalAnswer);
  const sentIds = sendText_(replyToken, userId, finalAnswer);
  sentIds.forEach(id => rememberBotMessageId_(state, id));
  saveState_(convKey, state);
}

// --- Prompt 組裝 / 判斷(對應 app/services/opencode_agent.py) ----------

function buildPrompt_(history, latestLabel, latestText, wasMentioned, isReplyToBot) {
  const contextLines = history.length
    ? history.map(([label, text]) => '[' + label + ']: ' + text).join('\n')
    : '(尚無對話紀錄)';
  let prompt =
    '以下是最近的對話紀錄(供判斷上下文用):\n' +
    contextLines +
    '\n\n最新訊息(請針對這一則回覆):\n' +
    '[' + latestLabel + ']: ' + latestText;

  if (wasMentioned || isReplyToBot) {
    const reason = wasMentioned
      ? '有人直接 @ 你本人,不是廣播 @所有人'
      : '有人直接回覆(swipe-to-reply)你之前傳的訊息';
    prompt +=
      '\n\n(提示:這則訊息' + reason + '。' +
      '既然是直接對你說話,請正常回覆、不要輸出 (silent),就算內容不完全' +
      '屬於總務處業務範圍,也請盡量給出有幫助的回應,或告知使用者' +
      '你能協助的範圍與正確窗口。)';
  }
  return prompt;
}

function shouldSuppress_(text) {
  if (text === null || text === undefined) return true;
  const stripped = String(text).trim();
  if (!stripped) return true;
  return SILENT_RE.test(stripped);
}

// --- Gemini 呼叫 ------------------------------------------------------

function callGeminiChain_(prompt, systemPrompt) {
  const apiKey = getProp_('GEMINI_API_KEY');
  for (const model of MODEL_CHAIN) {
    const answer = callGeminiOnce_(model, apiKey, prompt, systemPrompt);
    if (answer !== null) {
      return { text: answer, model: model };
    }
  }
  Logger.log('callGeminiChain_: all rungs failed');
  return null;
}

function callGeminiOnce_(model, apiKey, prompt, systemPrompt) {
  const url = 'https://generativelanguage.googleapis.com/v1beta/models/' + model + ':generateContent';
  const payload = {
    system_instruction: { parts: [{ text: systemPrompt || SYSTEM_PROMPT }] },
    contents: [{ role: 'user', parts: [{ text: prompt }] }],
  };
  let response;
  try {
    response = UrlFetchApp.fetch(url, {
      method: 'post',
      contentType: 'application/json',
      headers: { 'x-goog-api-key': apiKey },
      payload: JSON.stringify(payload),
      muteHttpExceptions: true,
    });
  } catch (err) {
    Logger.log('model=' + model + ' request failed: ' + err);
    return null;
  }

  const code = response.getResponseCode();
  if (code !== 200) {
    Logger.log('model=' + model + ' http ' + code + ': ' + response.getContentText().slice(0, 300));
    return null;
  }

  let data;
  try {
    data = JSON.parse(response.getContentText());
  } catch (err) {
    Logger.log('model=' + model + ' invalid JSON response');
    return null;
  }

  const candidate = (data.candidates || [])[0];
  const parts = candidate && candidate.content && candidate.content.parts;
  if (!parts || !parts.length) {
    Logger.log('model=' + model + ' produced no parseable answer');
    return null;
  }
  const text = parts.map(p => p.text || '').join('');
  return text || null;
}

// --- LINE API(對應 app/services/line_client.py) -----------------------

function sendText_(replyToken, to, text) {
  const replied = lineReply_(replyToken, text);
  if (replied !== null) return replied;
  Logger.log('Reply token invalid/expired, falling back to push for ' + to);
  const pushed = linePush_(to, text);
  return pushed || [];
}

function lineReply_(replyToken, text) {
  return linePost_('/message/reply', { replyToken: replyToken, messages: textMessages_(text) });
}

function linePush_(to, text) {
  return linePost_('/message/push', { to: to, messages: textMessages_(text) });
}

function linePost_(path, body) {
  let response;
  try {
    response = UrlFetchApp.fetch(LINE_API_BASE + path, {
      method: 'post',
      contentType: 'application/json',
      headers: { Authorization: 'Bearer ' + getProp_('LINE_CHANNEL_ACCESS_TOKEN') },
      payload: JSON.stringify(body),
      muteHttpExceptions: true,
    });
  } catch (err) {
    Logger.log('LINE ' + path + ' request failed: ' + err);
    return null;
  }
  if (response.getResponseCode() !== 200) {
    Logger.log('LINE ' + path + ' failed (' + response.getResponseCode() + '): ' + response.getContentText());
    return null;
  }
  return sentMessageIds_(response);
}

function textMessages_(text) {
  const trimmed = (text || '').trim() || '(空白回覆)';
  return [{ type: 'text', text: trimmed.slice(0, MAX_TEXT_LEN) }];
}

function sentMessageIds_(response) {
  try {
    const data = JSON.parse(response.getContentText());
    return (data.sentMessages || []).map(m => m.id).filter(Boolean);
  } catch (err) {
    return [];
  }
}

function getGroupMemberDisplayName_(state, groupId, userId) {
  if (state.displayNames[userId]) return state.displayNames[userId];
  const name = lineGet_('/group/' + groupId + '/member/' + userId);
  if (name) state.displayNames[userId] = name;
  return name;
}

function getUserProfileDisplayName_(state, userId) {
  if (state.displayNames[userId]) return state.displayNames[userId];
  const name = lineGet_('/profile/' + userId);
  if (name) state.displayNames[userId] = name;
  return name;
}

function lineGet_(path) {
  let response;
  try {
    response = UrlFetchApp.fetch(LINE_API_BASE + path, {
      headers: { Authorization: 'Bearer ' + getProp_('LINE_CHANNEL_ACCESS_TOKEN') },
      muteHttpExceptions: true,
    });
  } catch (err) {
    return null;
  }
  if (response.getResponseCode() !== 200) return null;
  try {
    return JSON.parse(response.getContentText()).displayName || null;
  } catch (err) {
    return null;
  }
}

// --- 對話狀態(對應 app/services/conversation.py,存 CacheService) ----

function loadState_(convKey) {
  const cache = CacheService.getScriptCache();
  const raw = cache.get(convKey);
  if (raw) {
    try {
      const parsed = JSON.parse(raw);
      return {
        history: parsed.history || [],
        userLabels: parsed.userLabels || {},
        nextLabelIndex: parsed.nextLabelIndex || 0,
        displayNames: parsed.displayNames || {},
        botMessageIds: parsed.botMessageIds || [],
      };
    } catch (err) {
      // 壞掉的快取內容,當成全新狀態處理。
    }
  }
  return { history: [], userLabels: {}, nextLabelIndex: 0, displayNames: {}, botMessageIds: [] };
}

function saveState_(convKey, state) {
  CacheService.getScriptCache().put(convKey, JSON.stringify(state), CACHE_TTL_SECONDS);
}

const LABEL_LETTERS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ';

function labelForUser_(state, userId) {
  if (!state.userLabels[userId]) {
    const letter = LABEL_LETTERS[state.nextLabelIndex % LABEL_LETTERS.length];
    state.userLabels[userId] = '使用者' + letter;
    state.nextLabelIndex += 1;
  }
  return state.userLabels[userId];
}

function addMessage_(state, label, text) {
  state.history.push([label, text]);
  if (state.history.length > CONTEXT_WINDOW) {
    state.history = state.history.slice(state.history.length - CONTEXT_WINDOW);
  }
}

function rememberBotMessageId_(state, messageId) {
  state.botMessageIds.push(messageId);
  if (state.botMessageIds.length > BOT_MESSAGE_ID_HISTORY) {
    state.botMessageIds = state.botMessageIds.slice(state.botMessageIds.length - BOT_MESSAGE_ID_HISTORY);
  }
}

// --- allowlist / 指令碼屬性 ---------------------------------------------

function rememberUnlistedGroup_(groupId) {
  const cache = CacheService.getScriptCache();
  const raw = cache.get('recent_unlisted_groups');
  let list = [];
  if (raw) {
    try { list = JSON.parse(raw); } catch (err) { list = []; }
  }
  if (list.indexOf(groupId) === -1) list.push(groupId);
  if (list.length > 20) list = list.slice(list.length - 20);
  cache.put('recent_unlisted_groups', JSON.stringify(list), CACHE_TTL_SECONDS);
}

function isGroupAllowed_(groupId) {
  if (!groupId) return false;
  const allowed = (getProp_('LINE_ALLOWED_GROUPS') || '')
    .split(',')
    .map(s => s.trim())
    .filter(Boolean);
  return allowed.indexOf(groupId) !== -1;
}

function getProp_(name) {
  return PropertiesService.getScriptProperties().getProperty(name) || '';
}

// --- 手動測試用(在 Apps Script 編輯器選這個函式按「執行」) -------------
//
// 不會真的發送 LINE 訊息,只呼叫 Gemini 驗證 SYSTEM_PROMPT / API key /
// callGeminiChain_ 這條路徑有沒有語法或設定錯誤,結果印在「執行紀錄」。
// 注意:函式名稱故意不加結尾底線——Apps Script 編輯器的「選取要執行的
// 函式」下拉選單會把結尾底線的函式當成私有輔助函式而不列出來,加了會
// 選不到、跑不了。
function manualTestCallGemini() {
  const result = callGeminiChain_(
    buildPrompt_([], '使用者A', '三樓廁所馬桶不通,麻煩派人來看一下', false, false)
  );
  Logger.log(JSON.stringify(result, null, 2));
}

// 印出最近被擋下(非白名單群組)的完整 groupId 清單,診斷/新增白名單用。
function debugRecentUnlistedGroups() {
  const raw = CacheService.getScriptCache().get('recent_unlisted_groups');
  Logger.log('recent_unlisted_groups: ' + (raw || '(空,還沒有任何非白名單群組的訊息被記錄到)'));
}

// 直接呼叫 LINE profile API,印出某個 userId 的真實 displayName 原始值,
// 用來確認「只回覆最後一個字」是不是 LINE API 本身回傳的值就是這樣。
function debugDisplayName() {
  const ownerId = getProp_('OWNER_USER_ID');
  Logger.log('OWNER_USER_ID=' + ownerId);
  const state = { displayNames: {} };
  const name = getUserProfileDisplayName_(state, ownerId);
  Logger.log('getUserProfileDisplayName_ result: ' + JSON.stringify(name));

  // 同時檢查 dm_<ownerId> 快取裡實際存的 displayNames 是不是舊的錯誤值
  // (CacheService 6 小時 TTL,查到就命中,不會重新打 LINE API)。
  const cached = loadState_('dm_' + ownerId);
  Logger.log('cached dm state displayNames: ' + JSON.stringify(cached.displayNames));
}

// 清掉 dm_<ownerId> 的快取,強迫下次私訊重新打 LINE API 拿最新 displayName。
function clearOwnerDmCache() {
  const ownerId = getProp_('OWNER_USER_ID');
  CacheService.getScriptCache().remove('dm_' + ownerId);
  Logger.log('cleared cache for dm_' + ownerId);
}
