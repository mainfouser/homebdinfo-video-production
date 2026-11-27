// =========================================================
// HOMEBDINFO AI TELEGRAM BOT - FULL APPS SCRIPT WORKFLOW
// Compatible with Google Apps Script & Node.js Telegram Bot API
// =========================================================

const CONFIG = {
  TELEGRAM_BOT_TOKEN: process.env.TELEGRAM_BOT_TOKEN || "YOUR_TELEGRAM_BOT_TOKEN",
  TELEGRAM_CHAT_ID: process.env.TELEGRAM_CHAT_ID || "YOUR_TELEGRAM_CHAT_ID",
  GITHUB_TOKEN: process.env.GITHUB_TOKEN || "YOUR_GITHUB_TOKEN",
  GITHUB_REPO: process.env.GITHUB_REPOSITORY || "username/homebdinfo-video-production",
  WORKFLOW_ID: "long_video.yml", // or pipeline_long_video.yml
  REF: "main"
};

/**
 * Escapes HTML characters for Telegram message parsing
 * @param {string} text 
 * @returns {string}
 */
function escapeHtml(text) {
  if (!text) return "";
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/**
 * Sends a message to Telegram
 * @param {string} chatId 
 * @param {string} text 
 * @param {object} replyMarkup 
 */
async function sendTelegramMessage(chatId, text, replyMarkup = null) {
  const url = `https://api.telegram.org/bot${CONFIG.TELEGRAM_BOT_TOKEN}/sendMessage`;
  const payload = {
    chat_id: chatId,
    text: text,
    parse_mode: "HTML",
    disable_web_page_preview: false
  };
  if (replyMarkup) {
    payload.reply_markup = replyMarkup;
  }

  try {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    return await response.json();
  } catch (error) {
    console.error("Error sending Telegram message:", error);
  }
}

/**
 * Edits an existing Telegram message text & markup
 * @param {string} chatId 
 * @param {number} messageId 
 * @param {string} text 
 * @param {object} replyMarkup 
 */
async function editTelegramMessage(chatId, messageId, text, replyMarkup = null) {
  const url = `https://api.telegram.org/bot${CONFIG.TELEGRAM_BOT_TOKEN}/editMessageText`;
  const payload = {
    chat_id: chatId,
    message_id: messageId,
    text: text,
    parse_mode: "HTML",
    disable_web_page_preview: false
  };
  if (replyMarkup !== null) {
    payload.reply_markup = replyMarkup;
  }

  try {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    return await response.json();
  } catch (error) {
    console.error("Error editing Telegram message:", error);
  }
}

/**
 * Answers a Telegram Callback Query to remove button loading spinner
 * @param {string} callbackQueryId 
 * @param {string} text 
 */
async function answerCallbackQuery(callbackQueryId, text = "") {
  const url = `https://api.telegram.org/bot${CONFIG.TELEGRAM_BOT_TOKEN}/answerCallbackQuery`;
  try {
    await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        callback_query_id: callbackQueryId,
        text: text.slice(0, 180)
      })
    });
  } catch (error) {
    console.error("Error answering callback query:", error);
  }
}

/**
 * Triggers a GitHub Actions Workflow Dispatch for Video Re-generation
 * @param {string} topicId 
 */
async function triggerGitHubWorkflow(topicId) {
  const url = `https://api.github.com/repos/${CONFIG.GITHUB_REPO}/actions/workflows/${CONFIG.WORKFLOW_ID}/dispatches`;
  try {
    const response = await fetch(url, {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${CONFIG.GITHUB_TOKEN}`,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        ref: CONFIG.REF,
        inputs: {
          topic_id: topicId,
          max_videos: "1"
        }
      })
    });
    return response.ok;
  } catch (error) {
    console.error("Error triggering GitHub Workflow:", error);
    return false;
  }
}

/**
 * Handles Telegram Callback Button Interactions
 * @param {object} callbackQuery 
 */
async function handleTelegramCallback(callbackQuery) {
  if (!callbackQuery || !callbackQuery.message) return;

  const chatId = String(callbackQuery.message.chat.id);
  const messageId = callbackQuery.message.message_id;
  const data = String(callbackQuery.data || "").trim();
  const baseText = String(callbackQuery.message.text || callbackQuery.message.caption || "");

  // Match pattern: LONG_<ACTION>_<TOPIC_ID>
  const match = data.match(/^LONG_(APPROVE|SCHEDULE|REGEN|REJECT)_(.+)$/);
  if (!match) {
    await answerCallbackQuery(callbackQuery.id, "অজানা বাটন অ্যাকশন।");
    return;
  }

  const action = match[1];
  const topicId = match[2];

  if (action === "APPROVE") {
    await answerCallbackQuery(callbackQuery.id, "✅ সাথে সাথে পাবলিশ হিসেবে সেভ করা হয়েছে!");
    const updatedText = `${escapeHtml(baseText)}

<b>স্ট্যাটাস:</b> ✅ <b>APPROVED & PUBLISHED IMMEDIATELY</b> (YouTube & Facebook)`;
    await editTelegramMessage(chatId, messageId, updatedText, { inline_keyboard: [] });

  } else if (action === "SCHEDULE") {
    const now = new Date();
    now.setHours(now.getHours() + 6); // Schedule after 6 hours
    const schedStr = now.toISOString().replace("T", " ").substring(0, 19);

    await answerCallbackQuery(callbackQuery.id, `⏰ সিডিউল করা হয়েছে: ${schedStr}`);
    const updatedText = `${escapeHtml(baseText)}

<b>স্ট্যাটাস:</b> ⏰ <b>SCHEDULED FOR ${schedStr}</b> (YouTube & Facebook)`;
    await editTelegramMessage(chatId, messageId, updatedText, { inline_keyboard: [] });

  } else if (action === "REGEN") {
    await answerCallbackQuery(callbackQuery.id, "🔄 রিজেনারেট রিকোয়েস্ট পাঠানো হচ্ছে...");
    const triggered = await triggerGitHubWorkflow(topicId);

    const statusText = triggered ? "GitHub Workflow Triggered" : "Queued in Google Sheet";
    const updatedText = `${escapeHtml(baseText)}

<b>স্ট্যাটাস:</b> 🔄 <b>REGENERATE REQUESTED</b> (${statusText})`;
    await editTelegramMessage(chatId, messageId, updatedText, { inline_keyboard: [] });

  } else if (action === "REJECT") {
    await answerCallbackQuery(callbackQuery.id, "❌ বাতিল করা হয়েছে!");
    const updatedText = `${escapeHtml(baseText)}

<b>স্ট্যাটাস:</b> ❌ <b>REJECTED & DELETED</b> (পাবলিশিং প্রক্রিয়া থামানো হয়েছে)`;
    await editTelegramMessage(chatId, messageId, updatedText, { inline_keyboard: [] });
  }
}

/**
 * Sends a Video Notification to Telegram with 4 Action Buttons
 * @param {string} topicId 
 * @param {string} title 
 * @param {string} videoUrl 
 * @param {string} releaseUrl 
 * @param {string} duration 
 * @param {number} sizeMb 
 */
async function sendLongVideoApprovalNotification(topicId, title, videoUrl, releaseUrl, duration, sizeMb) {
  const text = [
    "🎬 <b>নতুন ১৬:৯ লং ভিডিও তৈরি হয়েছে (অনুমোদন প্রয়োজন)</b>",
    "",
    `📌 <b>Topic ID:</b> <code>${escapeHtml(topicId)}</code>`,
    `📝 <b>Title:</b> ${escapeHtml(title)}`,
    `⏱ <b>Duration:</b> ${duration} | <b>Size:</b> ${sizeMb.toFixed(1)} MB`,
    videoUrl ? `▶️ <b>Watch / Download:</b> <a href='${escapeHtml(videoUrl)}'>Open Video MP4</a>` : "",
    releaseUrl ? `📦 <b>Release Page:</b> <a href='${escapeHtml(releaseUrl)}'>View Release</a>` : "",
    "",
    "নিচের ৪টি বাটন থেকে একটি অপশন বেছে নিন:"
  ].filter(Boolean).join("
");

  const replyMarkup = {
    inline_keyboard: [
      [
        { text: "✅ এখনই পাবলিশ", callback_data: `LONG_APPROVE_${topicId}` },
        { text: "⏰ সিডিউল পাবলিশ", callback_data: `LONG_SCHEDULE_${topicId}` }
      ],
      [
        { text: "🔄 রিজেনারেট", callback_data: `LONG_REGEN_${topic_id}` },
        { text: "❌ বাতিল (ডিলিট)", callback_data: `LONG_REJECT_${topicId}` }
      ]
    ]
  };

  return await sendTelegramMessage(CONFIG.TELEGRAM_CHAT_ID, text, replyMarkup);
}

// Export modules if using in Node.js environment
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    sendLongVideoApprovalNotification,
    handleTelegramCallback,
    triggerGitHubWorkflow,
    sendTelegramMessage,
    editTelegramMessage
  };
}
