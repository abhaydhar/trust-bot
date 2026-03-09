# Call Graph Validation Report

**Project ID**: 3191 | **Run ID**: 4970  
**Generated**: 2026-03-04 01:27 UTC  
**Source YAML**: 2026-03-04T01:26:48.778740+00:00

---

## Summary

| Metric | Count |
|---|---:|
| Total Snippets in YAML | 51 |
| Snippets with outgoing calls | 12 |
| Total YAML call edges | 16 |
| Confirmed in codebase | 0 |
| &nbsp;&nbsp;Full match (name+class+file) | 0 |
| &nbsp;&nbsp;Name+file match | 0 |
| &nbsp;&nbsp;Name-only match | 0 |
| Missing edge (function exists, no call edge) | 0 |
| Missing function (not in codebase at all) | 16 |
| Extra in codebase (not in YAML) | 20 |

**Coverage**: 0.0% of YAML edges confirmed in codebase

---

## Per-Snippet Detail

### ClickSurCategorie (fChoisirUnTarif.TChoisirUnTarif) — fChoisirUnTarif.pas
**Status**: 0/1 callees confirmed **GAPS**

**Missing from codebase:**

- `ChargeLaListe` (fChoisirUnTarif.TChoisirUnTarif, fChoisirUnTarif.pas) — Function 'ChargeLaListe' not found in codebase index

---

### Execute (fChoisirUnTarif.TChoisirUnTarif) — fChoisirUnTarif.pas
**Status**: 0/0 callees confirmed OK

**Extra in codebase (not in YAML):**

- `ThreadHasResumed` (TdxThread, dxLib_Thread.pas)
- `BeforeRun` (TdxThread, dxLib_Thread.pas)
- `ThreadIsActive` (TdxThread, dxLib_Thread.pas)
- `ThreadIsActive` (TdxThread, dxLib_Thread.pas)
- `DoOnRunCompletion` (TdxThread, dxLib_Thread.pas)
- `BetweenRuns` (TdxThread, dxLib_Thread.pas)
- `SuspendThread` (TdxThread, dxLib_Thread.pas)
- `AfterRun` (TdxThread, dxLib_Thread.pas)
- `WaitForResume` (TdxThread, dxLib_Thread.pas)
- `DoOnException` (TdxThread, dxLib_Thread.pas)

---

### Execute (fChoisirUnTarif.TChoisirUnTarif) — fChoisirUnTarif.pas
**Status**: 0/0 callees confirmed OK

**Extra in codebase (not in YAML):**

- `ThreadHasResumed` (TdxThread, dxLib_Thread.pas)
- `BeforeRun` (TdxThread, dxLib_Thread.pas)
- `ThreadIsActive` (TdxThread, dxLib_Thread.pas)
- `ThreadIsActive` (TdxThread, dxLib_Thread.pas)
- `DoOnRunCompletion` (TdxThread, dxLib_Thread.pas)
- `BetweenRuns` (TdxThread, dxLib_Thread.pas)
- `SuspendThread` (TdxThread, dxLib_Thread.pas)
- `AfterRun` (TdxThread, dxLib_Thread.pas)
- `WaitForResume` (TdxThread, dxLib_Thread.pas)
- `DoOnException` (TdxThread, dxLib_Thread.pas)

---

### ListeLignesitem1Click (fChoisirUnTarif.TChoisirUnTarif) — fChoisirUnTarif.pas
**Status**: 0/2 callees confirmed **GAPS**

**Missing from codebase:**

- `ChargeLaListe` (fChoisirUnTarif.TChoisirUnTarif, fChoisirUnTarif.pas) — Function 'ChargeLaListe' not found in codebase index
- `DoChoisirUnTarif` (fChoisirUnTarif.TChoisirUnTarif, fChoisirUnTarif.pas) — Function 'DoChoisirUnTarif' not found in codebase index

---

### btnAjouteDescendantClick (fMain.TfrmMain) — fMain.pas
**Status**: 0/1 callees confirmed **GAPS**

**Missing from codebase:**

- `AjouteCategorie` (uDBCategories.TdmDBCategories, uDBCategories.pas) — Function 'AjouteCategorie' not found in codebase index

---

### btnAjouteFrereClick (fMain.TfrmMain) — fMain.pas
**Status**: 0/1 callees confirmed **GAPS**

**Missing from codebase:**

- `AjouteCategorie` (uDBCategories.TdmDBCategories, uDBCategories.pas) — Function 'AjouteCategorie' not found in codebase index

---

### ChargeArborescence (fMain.TfrmMain) — fMain.pas
**Status**: 0/1 callees confirmed **GAPS**

**Missing from codebase:**

- `ChargeArborescence` (fMain.TfrmMain, fMain.pas) — Function 'ChargeArborescence' not found in codebase index

---

### DataModuleCreate (uDBCategories.TdmDBCategories) — uDBCategories.pas
**Status**: 0/2 callees confirmed **GAPS**

**Missing from codebase:**

- `OuvreBaseDeDonneesUtilisateur` (uDownloadAndGetFiles, uDownloadAndGetFiles.pas) — Function 'OuvreBaseDeDonneesUtilisateur' not found in codebase index
- `GetNomFichierExterne` (uFichiersEtDossiers, uFichiersEtDossiers.pas) — Function 'GetNomFichierExterne' not found in codebase index

---

### DataModuleCreate (uDBPourAffichage.TdmDBPourAffichage) — uDBPourAffichage.pas
**Status**: 0/2 callees confirmed **GAPS**

**Missing from codebase:**

- `OuvreBaseDeDonneesEnCache` (uDownloadAndGetFiles, uDownloadAndGetFiles.pas) — Function 'OuvreBaseDeDonneesEnCache' not found in codebase index
- `GetNomFichierExterne` (uFichiersEtDossiers, uFichiersEtDossiers.pas) — Function 'GetNomFichierExterne' not found in codebase index

---

### ChargeBitmapDansImage (uDownloadAndGetFiles) — uDownloadAndGetFiles.pas
**Status**: 0/1 callees confirmed **GAPS**

**Missing from codebase:**

- `GetNomFichierExterne` (uFichiersEtDossiers, uFichiersEtDossiers.pas) — Function 'GetNomFichierExterne' not found in codebase index

---

### OuvreBaseDeDonneesEnCache (uDownloadAndGetFiles) — uDownloadAndGetFiles.pas
**Status**: 0/1 callees confirmed **GAPS**

**Missing from codebase:**

- `GetNomFichierExterne` (uFichiersEtDossiers, uFichiersEtDossiers.pas) — Function 'GetNomFichierExterne' not found in codebase index

---

### OuvreBaseDeDonneesUtilisateur (uDownloadAndGetFiles) — uDownloadAndGetFiles.pas
**Status**: 0/1 callees confirmed **GAPS**

**Missing from codebase:**

- `GetNomFichierExterne` (uFichiersEtDossiers, uFichiersEtDossiers.pas) — Function 'GetNomFichierExterne' not found in codebase index

---

### Form1 (TForm1) — Unit1.dfm
**Status**: 0/2 callees confirmed **GAPS**

**Missing from codebase:**

- `Button2Click` (Unit1.TForm1, Unit1.pas) — Function 'Button2Click' not found in codebase index
- `TraitementDeLaBase` (Unit3, Unit3.pas) — Function 'TraitementDeLaBase' not found in codebase index

---

### Button2Click (Unit1.TForm1) — Unit1.pas
**Status**: 0/1 callees confirmed **GAPS**

**Missing from codebase:**

- `TraitementDeLaBase` (Unit3, Unit3.pas) — Function 'TraitementDeLaBase' not found in codebase index

---
