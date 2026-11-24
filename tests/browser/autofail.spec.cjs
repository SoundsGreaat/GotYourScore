const { expect, test } = require("@playwright/test");
const { randomUUID } = require("node:crypto");
const { base, nextResponse, signIn, watchPage } = require("./harness.cjs");

async function openNewReview(page, ruleKey = "ci-autofail-regular") {
  await signIn(page);
  // Keep the deduction UI deterministic without changing the DB's active rules.
  await page.route("**/partials/case-rules?*", (route) => route.fulfill({
    contentType: "text/html",
    body: `<div data-rule-row>
      <label><input type="checkbox" data-error-name="${ruleKey}" data-penalty="5"><span>Regular error</span></label>
      <input type="number" data-deduction min="1" max="5" value="5" aria-label="Deducted points" disabled>
    </div>`,
  }));
  await page.goto(`${base}/#qa-tracker/qa-score`);
  await page.getByRole("button", { name: "New Review", exact: true }).click();
  await page.locator("#review-agent-button").click();
  await page.locator("#review-agent-menu").getByRole("menuitemradio").first().click();
  await expect(page.getByRole("button", { name: "Save Review", exact: true })).toBeEnabled();
  return Number(await page.locator("#review-agent").inputValue());
}

async function chooseCaseType(page, name) {
  await page.locator("#case-type-button").click();
  await page.locator("#case-type-menu").getByRole("menuitemradio", { name, exact: true }).click();
}

test("Autofail previews instantly, validates by keyboard, and drafts notes without deductions", async ({ page }) => {
  const failures = watchPage(page);
  await page.setViewportSize({ width: 390, height: 820 });
  await openNewReview(page);
  const checkbox = page.getByRole("checkbox", { name: "Autofail", exact: true });
  const reason = page.getByRole("textbox", { name: "Autofail reason", exact: true });
  const score = page.getByLabel("Final score", { exact: true });
  const editor = page.locator("#quill-editor .ql-editor");
  const draftNotes = () => editor.click({ button: "right" });
  const notesAction = page.getByRole("button", { name: "Score → Notes", exact: true });
  const create = page.getByRole("button", { name: "Save Review", exact: true });
  const saveLater = page.getByRole("button", { name: "Save For Later", exact: true });

  await expect(score).toHaveText("100");
  await expect(reason).toBeHidden();
  let release;
  const held = new Promise((resolve) => { release = resolve; });
  await page.route("**/api/reviews/score-preview", async (route) => {
    if (route.request().postDataJSON().autofail) await held;
    await route.continue();
  });
  const preview = nextResponse(page, "/api/reviews/score-preview", "POST");
  try {
    await checkbox.focus();
    await page.keyboard.press("Space");
    await expect(checkbox).toBeChecked();
    await expect(checkbox).toBeFocused();
    await expect(score).toHaveText("0");
    await page.keyboard.press("Tab");
    await expect(reason).toBeFocused();
    await expect(reason).toHaveAttribute("required", "");
    await expect(reason).toHaveAttribute("maxlength", "2000");
    await expect(reason).toHaveAttribute("aria-invalid", "false");
    await create.press("Enter");
    await expect(reason).toBeFocused();
    await expect(reason).toHaveAccessibleDescription("Enter an Autofail reason.");
    await reason.fill("   ");
    await expect(reason).not.toHaveAttribute("aria-describedby", /.+/);
    await saveLater.press("Enter");
    await expect(reason).toBeFocused();
    await expect(reason).toHaveAttribute("aria-invalid", "true");
    await draftNotes();
    await notesAction.click();
    await expect(reason).toBeFocused();
    await expect(reason).toHaveAccessibleDescription("Enter an Autofail reason.");
  } finally {
    release();
  }
  const previewResponse = await preview;
  expect(previewResponse.status()).toBe(200);
  expect(previewResponse.request().postDataJSON()).toMatchObject({ autofail: true, raw_scorecard: {} });
  expect(previewResponse.request().postDataJSON()).not.toHaveProperty("autofail_reason");

  await page.route("**/api/ai/notes-from-score/stream", (route) => route.fulfill({
    contentType: "application/x-ndjson",
    body: `${JSON.stringify({ final: "<p>Drafted score notes.</p>" })}\n`,
  }));
  const reasonText = "<critical> policy violation";
  await reason.fill(`  ${reasonText}  `);
  await expect(reason).toHaveAttribute("aria-invalid", "false");
  const activeStream = nextResponse(page, "/api/ai/notes-from-score/stream", "POST");
  await draftNotes();
  await notesAction.click();
  expect((await activeStream).request().postDataJSON()).toMatchObject({
    raw_scorecard: {}, autofail_reason: reasonText,
  });
  await expect(editor).toHaveText("Drafted score notes.");
  await expect(notesAction).toBeHidden();

  await checkbox.press("Space");
  await expect(reason).toBeHidden();
  await expect(score).toHaveText("100");
  const regularStream = nextResponse(page, "/api/ai/notes-from-score/stream", "POST");
  await draftNotes();
  await notesAction.click();
  expect((await regularStream).request().postDataJSON()).not.toHaveProperty("autofail_reason");
  await expect(notesAction).toBeHidden();

  await checkbox.press("Space");
  await expect(reason).toHaveValue("");
  await reason.fill("Must be cleared when changing to No Cases");
  await chooseCaseType(page, "No Cases");
  await expect(checkbox).toBeHidden();
  await expect(reason).toBeHidden();
  await expect(score).toBeHidden();
  await chooseCaseType(page, "Initial Fix");
  await expect(checkbox).not.toBeChecked();
  await checkbox.press("Space");
  await expect(reason).toHaveValue("");
  await reason.fill(`  ${reasonText}  `);

  const creation = nextResponse(page, "/api/reviews", "POST");
  await create.click();
  const response = await creation;
  expect(response.status()).toBe(201);
  const review = await response.json();
  try {
    expect(response.request().postDataJSON()).toMatchObject({
      raw_scorecard: {}, autofail: true, autofail_reason: reasonText, save_for_later: false,
    });
    expect(review).toMatchObject({ autofail: true, autofail_reason: reasonText, final_score: 0 });
    await expect(page.locator("#review-drawer-toggle")).not.toBeChecked();
    expect(failures).toEqual([]);
  } finally {
    await page.request.delete(`${base}/api/reviews/${review.id}`);
  }
});

test("Autofail survives draft resume and completion without losing regular scoring selections", async ({ page }) => {
  const failures = watchPage(page);
  const ruleKey = `ci-autofail-${randomUUID()}`;
  const agentId = await openNewReview(page, ruleKey);
  const caseNumber = `CI Autofail ${randomUUID()}`;
  const reasonText = "Missed a critical check";
  const checkbox = page.getByRole("checkbox", { name: "Autofail", exact: true });
  const reason = page.getByRole("textbox", { name: "Autofail reason", exact: true });
  const score = page.getByLabel("Final score", { exact: true });
  const deduction = page.getByRole("spinbutton", { name: "Deducted points", exact: true });
  const rule = page.getByRole("checkbox", { name: "Regular error", exact: true });
  const multiplier = page.getByRole("checkbox", { name: /^Regular error ×/ });
  const supportReview = await page.request.post(`${base}/api/reviews`, {
    data: {
      support_agent_id: agentId, case_type: "Initial Fix", case_number: `CI history ${randomUUID()}`,
      raw_scorecard: { [ruleKey]: 4 },
    },
  });
  expect(supportReview.status()).toBe(201);
  const { id: historyId } = await supportReview.json();
  let reviewId;
  try {
    await page.getByRole("textbox", { name: "Case number", exact: true }).fill(caseNumber);
    await rule.check();
    await deduction.fill("4");
    await deduction.press("Tab");
    await expect(multiplier).toBeChecked();
    await expect(score).toHaveText("92");
    await multiplier.uncheck();
    await expect(score).toHaveText("96");
    await checkbox.check();
    await expect(score).toHaveText("0");
    await expect(rule).toBeChecked();
    await expect(multiplier).not.toBeChecked();
    await reason.fill(` ${reasonText} `);

    const creation = nextResponse(page, "/api/reviews", "POST");
    await page.getByRole("button", { name: "Save For Later", exact: true }).click();
    const response = await creation;
    expect(response.status()).toBe(201);
    const draft = await response.json();
    reviewId = draft.id;
    expect(draft).toMatchObject({
      status: "pending", final_score: null, autofail: true, autofail_reason: reasonText,
    });
    await expect(page.locator("#review-drawer-toggle")).not.toBeChecked();

    await page.getByRole("link", { name: /To review/i }).click();
    const open = page.locator("#to-review-view").getByRole("button", {
      name: `Complete delegated case ${caseNumber}`, exact: true,
    });
    await open.click();
    await expect(checkbox).toBeChecked();
    await expect(reason).toHaveValue(reasonText);
    await expect(rule).toBeChecked();
    await expect(deduction).toHaveValue("4");
    await expect(multiplier).toBeVisible();
    await expect(multiplier).not.toBeChecked();
    await expect(score).toHaveText("0");

    const complete = page.getByRole("button", { name: "Complete review", exact: true });
    const saveDraftChanges = page.getByRole("button", { name: "Save Changes", exact: true });
    await reason.fill(" ");
    await saveDraftChanges.press("Enter");
    await expect(reason).toBeFocused();
    await complete.press("Enter");
    await expect(reason).toBeFocused();
    await expect(reason).toHaveAccessibleDescription("Enter an Autofail reason.");
    await reason.fill(` ${reasonText} `);
    const saved = nextResponse(page, `/api/reviews/${reviewId}`, "PATCH");
    await saveDraftChanges.click();
    const savedResponse = await saved;
    expect(savedResponse.status()).toBe(200);
    expect(savedResponse.request().postDataJSON()).not.toHaveProperty("raw_scorecard");
    expect(await savedResponse.json()).toMatchObject({
      status: "pending", autofail: true, autofail_reason: reasonText,
    });

    await expect(page.locator("#review-drawer-toggle")).not.toBeChecked();
    await open.click();
    await expect(reason).toHaveValue(reasonText);
    const completion = nextResponse(page, `/api/reviews/${reviewId}`, "PATCH");
    await complete.click();
    const completedResponse = await completion;
    expect(completedResponse.status()).toBe(200);
    const completed = await completedResponse.json();
    expect(completed).toMatchObject({
      status: "completed", final_score: 0, autofail: true, autofail_reason: reasonText,
      scorecard_data: {
        breakdown: { [ruleKey]: { deducted: 4, multiplier: 1, final_penalty: 4 } },
        multiplier_exemptions: [ruleKey],
      },
    });
    await expect(page.locator("#review-drawer-toggle")).not.toBeChecked();

    await page.getByRole("link", { name: /All reviews/i }).click();
    const row = page.getByRole("row").filter({ hasText: caseNumber });
    await expect(row.getByRole("button", { name: `View notes for case ${caseNumber}`, exact: true })).toHaveText("0");
    await row.getByRole("button", { name: `Edit review ${reviewId}`, exact: true }).click();
    await expect(checkbox).toBeChecked();
    await expect(reason).toHaveValue(reasonText);
    const saveEdit = page.getByRole("button", { name: "Save changes", exact: true });
    await expect(saveEdit).toBeEnabled();
    await reason.fill("");
    await expect(reason).toHaveValue("");
    await saveEdit.press("Enter");
    await expect(reason).toBeFocused();
    await checkbox.uncheck();
    await expect(reason).toBeHidden();
    await expect(score).toHaveText("96");
    await expect(deduction).toHaveValue("4");
    await expect(multiplier).not.toBeChecked();
    const edit = nextResponse(page, `/api/reviews/${reviewId}`, "PATCH");
    await saveEdit.click();
    const editedResponse = await edit;
    expect(editedResponse.status()).toBe(200);
    expect(editedResponse.request().postDataJSON()).toMatchObject({ autofail: false, autofail_reason: null });
    expect(await editedResponse.json()).toMatchObject({
      final_score: 96, autofail: false, autofail_reason: null,
      scorecard_data: { breakdown: completed.scorecard_data.breakdown, multiplier_exemptions: [ruleKey] },
    });
    expect(failures).toEqual([]);
  } finally {
    if (reviewId) await page.request.delete(`${base}/api/reviews/${reviewId}`);
    await page.request.delete(`${base}/api/reviews/${historyId}`);
  }
});

test("a saved No Cases review can become an Autofail-only scored review", async ({ page }) => {
  const failures = watchPage(page);
  await openNewReview(page);
  const checkbox = page.getByRole("checkbox", { name: "Autofail", exact: true });
  const reason = page.getByRole("textbox", { name: "Autofail reason", exact: true });
  await checkbox.check();
  await reason.fill("Not saved with No Cases");
  await chooseCaseType(page, "No Cases");

  const creation = nextResponse(page, "/api/reviews", "POST");
  await page.getByRole("button", { name: "Save Review", exact: true }).click();
  const created = await creation;
  expect(created.status()).toBe(201);
  const review = await created.json();
  try {
    expect(created.request().postDataJSON()).toMatchObject({ autofail: false, autofail_reason: null });
    expect(review).toMatchObject({ case_type: "No Cases", autofail: false, autofail_reason: null, final_score: null });
    await expect(page.locator("#review-drawer-toggle")).not.toBeChecked();

    await page.getByRole("link", { name: /All reviews/i }).click();
    await page.getByRole("button", { name: `Edit review ${review.id}`, exact: true }).click();
    await expect(page.getByRole("heading", { name: `Edit review #${review.id}`, exact: true })).toBeVisible();
    await expect(checkbox).toBeHidden();
    await chooseCaseType(page, "Initial Fix");
    await expect(checkbox).not.toBeChecked();
    await checkbox.check();
    await expect(reason).toHaveValue("");
    await reason.fill("Critical error in the recovered case");
    await expect(page.getByLabel("Final score", { exact: true })).toHaveText("0");

    const edit = nextResponse(page, `/api/reviews/${review.id}`, "PATCH");
    await page.getByRole("button", { name: "Save changes", exact: true }).click();
    const edited = await edit;
    expect(edited.request().postDataJSON()).toMatchObject({ autofail: true, raw_scorecard: {} });
    expect(edited.status()).toBe(200);
    expect(await edited.json()).toMatchObject({ case_type: "Initial Fix", autofail: true, final_score: 0 });
    expect(failures).toEqual([]);
  } finally {
    await page.request.delete(`${base}/api/reviews/${review.id}`);
  }
});

test("Autofail keeps a legacy regular deduction named breakdown selected", async ({ page }) => {
  const agentId = await openNewReview(page, "breakdown");
  const seeded = await page.request.post(`${base}/api/reviews`, {
    data: {
      support_agent_id: agentId, case_type: "Initial Fix", case_number: `Legacy ${randomUUID()}`,
      raw_scorecard: { breakdown: 3 },
    },
  });
  expect(seeded.status()).toBe(201);
  const review = await seeded.json();
  const path = `/api/reviews/${review.id}`;
  try {
    // Mimic older reviews whose scorecard_data is itself the deduction map.
    await page.route(`**${path}`, (route) => route.fulfill({
      json: { ...review, scorecard_data: review.scorecard_data.breakdown },
    }));
    await page.locator(".gys-drawer-panel").getByText("Close", { exact: true }).click();
    await page.getByRole("link", { name: /All reviews/i }).click();
    await page.getByRole("button", { name: `Edit review ${review.id}`, exact: true }).click();
    const rule = page.getByRole("checkbox", { name: "Regular error", exact: true });
    await expect(rule).toBeChecked();
    await expect(page.getByRole("spinbutton", { name: "Deducted points", exact: true })).toHaveValue("3");
    await page.getByRole("checkbox", { name: "Autofail", exact: true }).check();
    await expect(rule).toBeChecked();
    await expect(page.getByLabel("Final score", { exact: true })).toHaveText("0");
  } finally {
    await page.request.delete(`${base}${path}`);
  }
});
