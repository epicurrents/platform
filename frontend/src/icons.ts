/**
 * Material Symbols — bundled SVG assets for the platform's base icon set.
 * Each icon is imported as a raw string (?raw) so Vite inlines it into the
 * build output; ``main.ts`` registers them with WebAwesome under the
 * ``default`` library and uses a mutator to set ``fill="currentColor"`` so
 * the icons inherit text colour.
 *
 * Material Symbols are copyright Google LLC and licensed under the
 * Apache License, Version 2.0 (https://www.apache.org/licenses/LICENSE-2.0).
 *
 * Adding a new icon:
 *   1. Import the SVG from ``@material-symbols/svg-400/outlined/<name>.svg?raw``.
 *   2. Add it to ``ICON_SVGS`` (Material name → SVG string).
 *   3. Map at least one FA-style kebab-case name to it in ``FA_TO_MATERIAL``.
 *
 * Project-specific icons live in ``projects/<project>/icons.ts``; base
 * entries win on conflict (see ``main.ts``).
 */
import add from '@material-symbols/svg-400/outlined/add.svg?raw'
import arrow_back from '@material-symbols/svg-400/outlined/arrow_back.svg?raw'
import arrow_forward from '@material-symbols/svg-400/outlined/arrow_forward.svg?raw'
import attach_file from '@material-symbols/svg-400/outlined/attach_file.svg?raw'
import check from '@material-symbols/svg-400/outlined/check.svg?raw'
import check_box from '@material-symbols/svg-400/outlined/check_box.svg?raw'
import check_box_outline_blank from '@material-symbols/svg-400/outlined/check_box_outline_blank.svg?raw'
import check_circle from '@material-symbols/svg-400/outlined/check_circle.svg?raw'
import chevron_right from '@material-symbols/svg-400/outlined/chevron_right.svg?raw'
import close from '@material-symbols/svg-400/outlined/close.svg?raw'
import cloud_upload from '@material-symbols/svg-400/outlined/cloud_upload.svg?raw'
import create_new_folder from '@material-symbols/svg-400/outlined/create_new_folder.svg?raw'
import dark_mode from '@material-symbols/svg-400/outlined/dark_mode.svg?raw'
import database from '@material-symbols/svg-400/outlined/database.svg?raw'
import delete_ from '@material-symbols/svg-400/outlined/delete.svg?raw'
import description from '@material-symbols/svg-400/outlined/description.svg?raw'
import download from '@material-symbols/svg-400/outlined/download.svg?raw'
import desktop_windows from '@material-symbols/svg-400/outlined/desktop_windows.svg?raw'
import directory_sync from '@material-symbols/svg-400/outlined/directory_sync.svg?raw'
import edit from '@material-symbols/svg-400/outlined/edit.svg?raw'
import error from '@material-symbols/svg-400/outlined/error.svg?raw'
import file_copy from '@material-symbols/svg-400/outlined/file_copy.svg?raw'
import folder from '@material-symbols/svg-400/outlined/folder.svg?raw'
import folder_copy from '@material-symbols/svg-400/outlined/folder_copy.svg?raw'
import folder_open from '@material-symbols/svg-400/outlined/folder_open.svg?raw'
import format_list_numbered from '@material-symbols/svg-400/outlined/format_list_numbered.svg?raw'
import group from '@material-symbols/svg-400/outlined/group.svg?raw'
import home from '@material-symbols/svg-400/outlined/home.svg?raw'
import info from '@material-symbols/svg-400/outlined/info.svg?raw'
import key from '@material-symbols/svg-400/outlined/key.svg?raw'
import light_mode from '@material-symbols/svg-400/outlined/light_mode.svg?raw'
import link from '@material-symbols/svg-400/outlined/link.svg?raw'
import lock from '@material-symbols/svg-400/outlined/lock.svg?raw'
import lock_open from '@material-symbols/svg-400/outlined/lock_open.svg?raw'
import menu_book from '@material-symbols/svg-400/outlined/menu_book.svg?raw'
import monitor_heart from '@material-symbols/svg-400/outlined/monitor_heart.svg?raw'
import more_horiz from '@material-symbols/svg-400/outlined/more_horiz.svg?raw'
import movie from '@material-symbols/svg-400/outlined/movie.svg?raw'
import notifications from '@material-symbols/svg-400/outlined/notifications.svg?raw'
import open_in_new from '@material-symbols/svg-400/outlined/open_in_new.svg?raw'
import person from '@material-symbols/svg-400/outlined/person.svg?raw'
import play_arrow from '@material-symbols/svg-400/outlined/play_arrow.svg?raw'
import play_circle from '@material-symbols/svg-400/outlined/play_circle.svg?raw'
import progress_activity from '@material-symbols/svg-400/outlined/progress_activity.svg?raw'
import save from '@material-symbols/svg-400/outlined/save.svg?raw'
import schedule from '@material-symbols/svg-400/outlined/schedule.svg?raw'
import school from '@material-symbols/svg-400/outlined/school.svg?raw'
import share from '@material-symbols/svg-400/outlined/share.svg?raw'
import tune from '@material-symbols/svg-400/outlined/tune.svg?raw'
import vital_signs from '@material-symbols/svg-400/outlined/vital_signs.svg?raw'
import warning from '@material-symbols/svg-400/outlined/warning.svg?raw'

const ICON_SVGS: Record<string, string> = {
    add,
    arrow_back,
    arrow_forward,
    attach_file,
    check,
    check_box,
    check_box_outline_blank,
    check_circle,
    chevron_right,
    close,
    cloud_upload,
    create_new_folder,
    dark_mode,
    database,
    delete: delete_,
    description,
    desktop_windows,
    directory_sync,
    download,
    edit,
    error,
    file_copy,
    folder,
    folder_copy,
    folder_open,
    format_list_numbered,
    group,
    home,
    info,
    key,
    light_mode,
    link,
    lock,
    lock_open,
    menu_book,
    monitor_heart,
    more_horiz,
    movie,
    notifications,
    open_in_new,
    person,
    play_arrow,
    play_circle,
    progress_activity,
    save,
    schedule,
    school,
    share,
    tune,
    vital_signs,
    warning,
}

/**
 * Maps the FA-style kebab-case names used throughout the platform UI to
 * Material Symbols names. New code may use the Material name directly
 * (``<wa-icon name="info">``); existing call sites continue to work via
 * this table.
 */
const FA_TO_MATERIAL: Record<string, string> = {
    'angle-right':                 'chevron_right',
    'arrow-left':                  'arrow_back',
    'arrow-right':                 'arrow_forward',
    'arrow-up-right-from-square':  'open_in_new',
    'arrows-rotate':               'directory_sync',
    'bell':                        'notifications',
    'book-open':                   'menu_book',
    'check':                       'check',
    'circle-check':                'check_circle',
    'circle-exclamation':          'error',
    'circle-info':                 'info',
    'clock':                       'schedule',
    'cloud-arrow-up':              'cloud_upload',
    'database':                    'database',
    'display':                     'desktop_windows',
    'download':                    'download',
    'ellipsis':                    'more_horiz',
    'file':                        'description',
    'file-music':                  'monitor_heart',
    'files':                       'file_copy',
    'film':                        'movie',
    'floppy-disk':                 'save',
    'folder':                      'folder',
    'folder-open':                 'folder_open',
    'folder-plus':                 'create_new_folder',
    'folders':                     'folder_copy',
    'graduation-cap':              'school',
    'home':                        'home',
    'key':                         'key',
    'link-simple':                 'link',
    'list-ol':                     'format_list_numbered',
    'lock':                        'lock',
    'lock-open':                   'lock_open',
    'moon':                        'dark_mode',
    'paperclip':                   'attach_file',
    'pencil':                      'edit',
    'play':                        'play_arrow',
    'play-circle':                 'play_circle',
    'plus':                        'add',
    'share':                       'share',
    'sliders':                     'tune',
    'spinner':                     'progress_activity',
    'square':                      'check_box_outline_blank',
    'square-check':                'check_box',
    'sun':                         'light_mode',
    'trash':                       'delete',
    'triangle-exclamation':        'warning',
    'user':                        'person',
    'users':                       'group',
    'waveform-lines':              'vital_signs',
    'xmark':                       'close',
}

// Build the flat FA-name → SVG-string lookup that ``main.ts`` consumes. Any
// Material name that exists in ``ICON_SVGS`` but isn't referenced by
// ``FA_TO_MATERIAL`` is unused — keep them in sync.
const icons: Record<string, string> = {}
for (const [faName, materialName] of Object.entries(FA_TO_MATERIAL)) {
    const svg = ICON_SVGS[materialName]
    if (svg) {
        icons[faName] = svg
    }
}

export default icons
