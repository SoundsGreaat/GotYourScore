const { expect, test } = require("@playwright/test");
const { randomUUID } = require("node:crypto");
const { base, nextResponse, signIn } = require("./harness.cjs");

for (const [tracker, name] of [
  ["bad-feedback", "Bad Feedback"],
  ["refund-check", "Refund Check"],
]) {
  test(`${name} configures, validates, displays, and edits precise numeric fields`, async ({ page }) => {
    if (tracker === "refund-check") await page.setViewportSize({ width: 390, height: 844 });
    await signIn(page);
    await page.goto(`${base}/admin#case-fields`);
    const region = page.getByRole("region", { name, exact: true });
    const builtIn = region.getByRole("form", { name: "Edit Source", exact: true });
    await region.locator("summary").getByText("Source", { exact: true }).click();
    await builtIn.getByRole("button", { name: "Type", exact: true }).press("ArrowDown");
    await expect(builtIn.getByRole("menuitemradio", { name: "Text", exact: true })).toBeVisible();
    await expect(builtIn.getByRole("menuitemradio", { name: /Integer|Decimal/ })).toHaveCount(0);
    await page.keyboard.press("Escape");

    const suffix = randomUUID().slice(0, 8);
    const fieldIds = [];
    const recordPath = `/api/${tracker}`;
    const adminPath = `/admin/case-fields/${tracker}`;
    let recordId;
    try {
      await region.locator("[data-case-default-focus]").press("Enter");
      const add = region.getByRole("form", { name: `Add ${name} field` });
      async function addField(label, type) {
        await add.getByLabel("Label").fill(label);
        const picker = add.getByRole("button", { name: "Type", exact: true });
        await picker.press("ArrowDown");
        await page.keyboard.press("End");
        if (type === "Integer") await page.keyboard.press("ArrowUp");
        await expect(add.getByRole("menuitemradio", { name: type, exact: true })).toBeFocused();
        await page.keyboard.press("Enter");
        if (type === "Integer") {
          await add.getByLabel("Show in My Reviews").check();
          await add.getByLabel("Required for import").check();
        }
        const created = nextResponse(page, `${adminPath}/create`, "POST");
        await add.getByRole("button", { name: "Add field" }).press("Enter");
        expect((await created).status()).toBe(200);

        const row = region.getByRole("listitem").filter({
          has: page.getByRole("group", { name: `Reorder ${label}`, exact: true }),
        });
        await expect(row.locator("summary").getByText(type, { exact: true })).toBeVisible();
        fieldIds.push(await row.getAttribute("data-case-field-id"));
        if (type === "Integer") {
          await row.locator("summary").press("Enter");
          const edit = row.getByRole("form", { name: `Edit ${label}`, exact: true });
          await expect(edit.getByLabel("Show in My Reviews")).toBeChecked();
          await expect(edit.getByLabel("Required for import")).toBeChecked();
        }
        return row.locator("code").first().textContent();
      }
      const integerLabel = `Units ${suffix}`;
      const decimalLabel = `Amount ${suffix}`;
      const integerKey = await addField(integerLabel, "Integer");
      const decimalKey = await addField(decimalLabel, "Decimal");

      await page.goto(`${base}/#qa-tracker/${tracker}`);
      await page.getByRole("button", { name: `New ${name}`, exact: true }).press("Enter");
      const create = page.getByRole("dialog", { name: `New ${name}`, exact: true });
      const integer = create.getByRole("textbox", { name: integerLabel, exact: true });
      const decimal = create.getByRole("textbox", { name: decimalLabel, exact: true });
      const saveLater = create.getByRole("button", { name: "Save For Later" });
      const precise = "9007199254740993.123456789";
      await integer.fill("2.01");
      await integer.press("Tab");
      await expect(decimal).toBeFocused();
      await decimal.fill(precise);
      const rejected = nextResponse(page, recordPath, "POST");
      await saveLater.press("Enter");
      expect((await rejected).status()).toBe(400);
      await expect(integer).toBeFocused();
      await expect(integer).toHaveAttribute("aria-invalid", "true");
      await expect(integer).toHaveAccessibleDescription(/Enter a valid whole number/);
      await expect(integer).toHaveValue("2.01");
      await expect(decimal).toHaveValue(precise);

      await integer.fill("0");
      await expect(integer).not.toHaveAttribute("aria-invalid");
      const created = nextResponse(page, recordPath, "POST");
      await saveLater.press("Enter");
      const createdResponse = await created;
      expect(createdResponse.status()).toBe(201);
      const record = await createdResponse.json();
      recordId = record.id;
      expect(createdResponse.request().postDataJSON().field_values).toMatchObject({
        [integerKey]: "0", [decimalKey]: precise,
      });
      expect(record.field_values).toMatchObject({ [integerKey]: "0", [decimalKey]: precise });
      await expect(create).toBeHidden();

      const editButton = page.getByRole("button", { name: `Edit ${name.toLowerCase()} ${recordId}`, exact: true });
      const recordRow = page.getByRole("row").filter({ has: editButton });
      await expect(recordRow.getByRole("cell", { name: "0", exact: true })).toBeVisible();
      await expect(recordRow.getByRole("cell", { name: precise, exact: true })).toBeVisible();
      await editButton.press("Enter");
      const edit = page.getByRole("dialog", { name: `Edit ${name}`, exact: true });
      const editInteger = edit.getByRole("textbox", { name: integerLabel, exact: true });
      const editDecimal = edit.getByRole("textbox", { name: decimalLabel, exact: true });
      const save = edit.getByRole("button", { name: "Save", exact: true });
      await expect(editInteger).toHaveValue("0");
      await expect(editDecimal).toHaveValue(precise);
      const { scrollWidth, clientWidth } = await edit.evaluate((node) =>
        ({ scrollWidth: node.scrollWidth, clientWidth: node.clientWidth }));
      expect(scrollWidth).toBeLessThanOrEqual(clientWidth);

      await editDecimal.fill("NaN");
      const rejectedEdit = nextResponse(page, `${recordPath}/${recordId}`, "PATCH");
      await save.press("Enter");
      expect((await rejectedEdit).status()).toBe(400);
      await expect(editDecimal).toBeFocused();
      await expect(editDecimal).toHaveAccessibleDescription(/Enter a valid number using a dot for decimals/);
      await editDecimal.fill("");
      await editInteger.fill("-9007199254740993");
      const updated = nextResponse(page, `${recordPath}/${recordId}`, "PATCH");
      await save.press("Enter");
      const updatedResponse = await updated;
      expect(updatedResponse.status()).toBe(200);
      expect(updatedResponse.request().postDataJSON().field_values).toMatchObject({
        [integerKey]: "-9007199254740993", [decimalKey]: null,
      });
      await expect(edit).toBeHidden();
      await expect(recordRow.getByRole("cell", { name: "-9007199254740993", exact: true })).toBeVisible();
      await editButton.press("Enter");
      await expect(editInteger).toHaveValue("-9007199254740993");
      await expect(editDecimal).toHaveValue("");
    } finally {
      if (recordId) await page.request.delete(`${base}${recordPath}/${recordId}`);
      for (const id of fieldIds) {
        await page.request.post(`${base}${adminPath}/${id}/archive`, { headers: { "HX-Request": "true" } });
      }
    }
  });

  test(`${name} maps numeric Excel fields and safely reports skipped rows`, async ({ page }) => {
    await signIn(page);
    const importPath = `/api/${tracker}/import`;
    const amountLabel = "Amount <USD>";
    await page.route(`**${importPath}/inspect`, (route) => route.fulfill({
      json: {
        headers: ["Count", "Exact total"],
        field_labels: { custom_units: "Units", custom_amount: amountLabel },
        required_fields: [],
        suggestions: {},
        total_rows: 3,
        preview: [
          { row_number: 2, values: ["0", "9007199254740993.123456789"] },
          { row_number: 3, values: ["1.5", "0"] },
          { row_number: 4, values: ["2", "Non-numeric cell"] },
        ],
      },
    }));
    await page.route(`**${importPath}`, (route) => route.fulfill({
      json: {
        created_count: 1,
        skipped_count: 2,
        created_users: [],
        skipped: [
          { row_number: 3, code: "invalid_integer", field_key: "custom_units", reason: "DO NOT RENDER THIS" },
          { row_number: 4, code: "invalid_decimal", field_key: "custom_amount", reason: "DO NOT RENDER THIS" },
        ],
      },
    }));

    await page.goto(`${base}/#qa-tracker/${tracker}`);
    await page.getByRole("button", { name: "Import from Excel" }).press("Enter");
    const dialog = page.getByRole("dialog", { name: `Import ${name}`, exact: true });
    await dialog.getByLabel("Choose an .xlsx file").setInputFiles({
      name: "numeric.xlsx", mimeType: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      buffer: Buffer.from("fixture"),
    });
    await dialog.getByRole("button", { name: "Analyze" }).press("Enter");
    await expect(dialog.getByRole("cell", { name: "9007199254740993.123456789", exact: true })).toBeVisible();
    for (const [label, header] of [["Units", "Count"], [amountLabel, "Exact total"]]) {
      await dialog.getByRole("button", { name: `Column for ${label}`, exact: true }).press("ArrowDown");
      await dialog.getByRole("menuitemradio", { name: header, exact: true }).click();
    }
    const committed = nextResponse(page, importPath, "POST");
    await dialog.getByRole("button", { name: "Import", exact: true }).press("Enter");
    expect((await committed).request().postData()).toContain(
      JSON.stringify({ custom_units: "Count", custom_amount: "Exact total" }));
    const done = dialog.getByRole("button", { name: "Done", exact: true });
    await expect(done).toBeFocused();
    await expect(done).toHaveAccessibleDescription("1 record(s) imported. 2 skipped.");
    await expect(dialog.getByText("Row 3: Units — Enter a valid whole number.", { exact: true })).toBeVisible();
    await expect(dialog.getByText(`Row 4: ${amountLabel} — Enter a valid number using a dot for decimals.`,
      { exact: true })).toBeVisible();
    await expect(dialog.getByText("DO NOT RENDER THIS")).toHaveCount(0);
    await done.press("Enter");
    await expect(dialog).toBeHidden();
  });
}
