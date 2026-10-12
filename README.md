# Student Photo Tagger

Select a student, take the photo, and Smart Shooter 5 saves it with that student's number and name.
Afterwards, export clean copies **By Name** and/or **By ID**. It can also convert old photos that are
named by student number.

## Download

Open the **Actions** tab → newest **Build Windows app** run → **Artifacts** → `StudentPhotoTagger`.
Unzip it to get `StudentPhotoTagger.exe`. Version releases also appear under **Releases**.

The first time you run it, Windows may say *"Windows protected your PC"*. Click **More info → Run anyway**.
This happens because the app isn't digitally signed.

## One-time setup (5 minutes)

1. **Turn on Smart Shooter's External API.** In Smart Shooter 5 open **File → Options** and find the
   **External API** setting. Turn it on and restart Smart Shooter. If you can't find it, your
   Smart Shooter edition may not include it — check with Kuvacode/Tether Tools.
2. **Start Smart Shooter**, connect the camera, then start **StudentPhotoTagger.exe**.
   The top-right corner should say **● Smart Shooter connected**. If Windows Firewall asks, click **Allow**.
3. In the app, choose **Smart Shooter → Set up file naming** and click **Yes**.
   Photos will now be saved like `10234_Nimal_Perera.jpg`.
4. Click **Load student list** and pick your CSV or Excel file (a TASS export works). It needs columns for
   student number, first name and last name. Class and Campus columns are used if present.
   See `sample_students.csv` for the layout. The app remembers the list next time.

## Choosing how Smart Shooter names photos

The **Smart Shooter file names** tick boxes under the toolbar decide what goes in each file name:

| Ticked | Photos are saved as |
| --- | --- |
| Student number + Student name | `10234_Nimal_Perera.jpg` |
| Student number only | `10234.jpg` |
| Student name only | `Nimal_Perera.jpg` (two students with the same name also get their number) |

If a student has more than one photo, Smart Shooter adds a number to the later ones so nothing is
overwritten. Tick **Add shot number** if you'd rather every photo is numbered (`10234_001.jpg`, `10234_002.jpg`).

At least one of number/name must stay ticked. A change applies from the next photo and is remembered. Export and
Convert recognise all three styles, so you can mix them in one folder.

## Picture day

1. Type a few letters of the name in **Search** and press **Enter** (or click the name).
2. Wait for the banner to turn **green**: that means Smart Shooter has the student's name.
3. Take the photos. Each one appears under **Activity** and the student gets a ✓ and a count.
4. Click **Next student ▶** (or Ctrl+N) to move down the list.

**Sorting:** use **Sort by** (Campus, Surname, First name, Student number, Class, Photos) or click a column
heading; click it again to reverse. **Show** limits the list to one campus. **Next student** follows the order on screen.

Banner colours:

| Colour | Meaning |
| --- | --- |
| Green | Ready — photos will be named after this student |
| Amber | Waiting for Smart Shooter to confirm |
| Red | Smart Shooter isn't connected — photos will NOT be named |
| Grey | No student selected — photos won't be linked to anyone |

A photo saved with no student selected is shown in red in **Activity** and the computer beeps.

### Checking photos and choosing the best one

- **Last photo** (right of the banner) shows each photo as soon as Smart Shooter saves it, with the student
  it was linked to. If it says **NOT linked to a student** in red, no student was selected. Click it to open the photo.
- **Photos of the selected student** (right side) shows that student's photos, newest first.
  Click a photo to **★ star** it as the best one; click it again to remove the star.
  Export uses the starred photo instead of the newest. Stars are saved in `best_photos.json` in the photo folder.
- Selecting a student who already has photos says so in **Activity**.

### Adding a student on the day

Click **+ Add student** next to the search box, enter the number, name and class, then **Add and select**.
Added students show "(added)" in blue and stay in the list each time you open the app, until your school's
list includes them. **Reports → Save added students list** gives the office a CSV to add them to TASS.

## Exporting

**Export photos** copies photos from Smart Shooter's folder. **Convert existing photos** does the same for
any folder of photos named by student number (`10234.jpg`, `10234_2.jpg`, `10234 (1).jpg`,
`010234.jpg` all match student 10234).

1. Under **Photo folders**, click **Add folder…** for each folder of photos (add as many as you like;
   tick **Include subfolders** to look inside them too). Choose **Export to** (a different, empty folder is best).
2. Pick **By Name**, **By ID** or both; tick **In campus folders** and/or **In class folders** to sort copies by the list's Campus and Class columns;
   **one photo per student** (the starred photo, otherwise newest or first) or **all**; and the name format (`Nimal Perera.jpg` or `Perera, Nimal.jpg`).
3. Click **Preview** to see every file and its new name, then **Export**.

You get `By ID\10234.jpg` and `By Name\Nimal Perera.jpg` (or `By Name\North\7B\Nimal Perera.jpg` with campus and class folders;
students with no campus or class go in `No campus` / `No class`). The same student found in several folders counts as several photos,
so **One (newest)** picks the most recent across all folders. Students with the same name get their number
added, e.g. `Nimal Perera (10234).jpg`. RAW + JPG pairs stay together. Originals are never changed.

The export folder also gets `export_report.csv` (every copy plus unmatched files) and
`students_without_photo.csv`.

**Reports** in the main window saves the day's photo log and a not-yet-photographed list.

## History

The app keeps a history database on this PC (`%APPDATA%\StudentPhotoTagger\history.db`) of every photo,
which student it belongs to, stars, added students and exports, across all picture days. Click **History**:

| Tab | Shows |
| --- | --- |
| Students | Every student ever loaded, with all their photos across years, the starred one, and how often each was exported. Double-click to open a photo. |
| Picture days | Each day photos were taken: counts, unlinked photos, and every photo that day. |
| Exports | Every export and convert, with the folder (double-click to open it). |
| Activity | Who was selected, photos saved, stars, added students, settings changes. |

**Save full history as CSV** gives a spreadsheet of every photo. Back up `history.db` with the photos if you
want to keep history when replacing the PC.

## Troubleshooting

| Problem | Fix |
| --- | --- |
| "Smart Shooter not connected" | Start Smart Shooter, check External API is on, then **Smart Shooter → Reconnect**. |
| Banner stays amber | Same as above; the app keeps retrying on its own. |
| Photos named without the student | Run **Smart Shooter → Set up file naming**, and turn off *Auto Scan Barcodes* in Smart Shooter so a scanned code can't replace the name. |
| Smart Shooter is on another computer | **Smart Shooter → Connection address** and enter that computer's IP address. |
| List won't load | It needs student number, first name and last name columns. Save as CSV and try again. |

## For developers

Python 3.10+ with `pip install -r requirements.txt`, then `python run.py`.
Tests: `python -m unittest discover -s tests -t .` (the integration test runs the real window against
`tests/fake_smartshooter.py`, a simulated Smart Shooter you can also run by hand).
`build.bat` builds the exe locally; GitHub Actions builds it on every push.

Smart Shooter integration uses its [External API](https://github.com/kuvacode/smartshooter-api):
`SetOptions` with `Barcode` sets the `[Z]` filename field, and `PhotoUpdated` events report saved photos.
