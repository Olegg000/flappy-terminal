# Flappy Bird for the terminal - installer for Windows (PowerShell 5.1+ or PowerShell 7).
#
#   Install / update:
#     irm https://raw.githubusercontent.com/Olegg000/flappy-terminal/main/install.ps1 | iex
#   Uninstall:
#     $env:FLAPPY_UNINSTALL=1; irm https://raw.githubusercontent.com/Olegg000/flappy-terminal/main/install.ps1 | iex
#
# What it does: finds Python 3.9+ (offers to install it with winget when there is none), installs the
# windows-curses package for that Python, downloads flappy.py to %LOCALAPPDATA%\flappy-terminal, creates
# a `flappy` command there and adds its folder to your user PATH.  No admin rights needed.

& {
    # Windows PowerShell 5.1 turns a native program's stderr into errors, so 'Stop' would abort on harmless
    # pip/python warnings: errors are handled explicitly instead.  Python code passed to python.exe never
    # contains double quotes, because 5.1 does not escape them for native commands.
    $ErrorActionPreference = 'Continue'
    $ProgressPreference = 'SilentlyContinue'      # Invoke-WebRequest is very slow with the progress bar on 5.1
    try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch { }

    $RawUrl  = if ($env:FLAPPY_RAW_URL) { $env:FLAPPY_RAW_URL } else { 'https://raw.githubusercontent.com/Olegg000/flappy-terminal/main' }
    $AppDir  = Join-Path $env:LOCALAPPDATA 'flappy-terminal'
    $BinDir  = Join-Path $AppDir 'bin'
    $Game    = Join-Path $AppDir 'flappy.py'
    $Command = Join-Path $BinDir 'flappy.cmd'

    function Step($t) { Write-Host '==> ' -ForegroundColor Cyan -NoNewline; Write-Host $t }
    function Ok($t)   { Write-Host '  + ' -ForegroundColor Green -NoNewline; Write-Host $t }
    function Warn($t) { Write-Host '  ! ' -ForegroundColor Yellow -NoNewline; Write-Host $t }
    function Fail($t) { Write-Host "error: $t" -ForegroundColor Red }

    # User PATH is edited in the registry as REG_EXPAND_SZ so entries like %USERPROFILE%\bin survive.
    # Get-UserPath throws when the registry cannot be read, so a failed read can never lead to writing a
    # PATH that lost the user's existing entries.
    function Get-UserPath {
        $key = Get-Item -Path 'HKCU:\Environment' -ErrorAction Stop
        return [string]$key.GetValue('Path', '', 'DoNotExpandEnvironmentNames')
    }
    function Set-UserPath([string]$value) {
        Set-ItemProperty -Path 'HKCU:\Environment' -Name 'Path' -Value $value -Type ExpandString -ErrorAction Stop
        # Tell Explorer and new terminals that the environment changed (setting a dummy variable broadcasts it).
        [Environment]::SetEnvironmentVariable('FLAPPY_TERMINAL_REFRESH', '1', 'User')
        [Environment]::SetEnvironmentVariable('FLAPPY_TERMINAL_REFRESH', $null, 'User')
    }
    function Split-PathList([string]$value) { return @($value -split ';' | Where-Object { $_ -ne '' }) }

    if ($env:FLAPPY_UNINSTALL) {
        Remove-Item Env:FLAPPY_UNINSTALL -ErrorAction SilentlyContinue
        Step 'Removing Flappy Bird'
        if (Test-Path $AppDir) { Remove-Item -Recurse -Force $AppDir }
        try {
            $parts = Split-PathList (Get-UserPath)
            if ($parts -contains $BinDir) { Set-UserPath (($parts | Where-Object { $_ -ne $BinDir }) -join ';') }
        } catch {
            Warn "could not edit your user PATH ($($_.Exception.Message)); remove $BinDir from it by hand"
        }
        $env:Path = ((Split-PathList $env:Path) | Where-Object { $_ -ne $BinDir }) -join ';'
        Ok "removed $AppDir"
        Write-Host '  Your best scores stay in %USERPROFILE%\.flappy_term.json; the windows-curses package stays installed.'
        return
    }

    Write-Host ''
    Write-Host '  Flappy Bird for the terminal - installer' -ForegroundColor White
    Write-Host ''

    # --- 1. Python 3.9+ ---------------------------------------------------------------------------
    # Each candidate is an executable plus arguments.  The Microsoft Store "python.exe" alias only prints
    # a hint and fails, so every candidate is actually run before it is trusted.
    function Find-Pythons {
        $found = [System.Collections.Generic.List[string]]::new()
        $cands = @(
            @('py', '-3'), @('python'), @('python3'),
            @((Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe')),
            @((Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'))
        )
        foreach ($c in $cands) {
            $exe = $c[0]; $extra = @($c | Select-Object -Skip 1)
            if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
            try {
                $out = & $exe @extra -c 'import sys; assert sys.version_info >= (3, 9); print(sys.executable)' 2>$null
            } catch { continue }
            if ($LASTEXITCODE -ne 0 -or -not $out) { continue }
            $path = ([string]($out | Select-Object -Last 1)).Trim()
            if ($path -and (Test-Path $path) -and -not $found.Contains($path)) { $found.Add($path) }
        }
        return $found
    }

    # windows-curses is a separate package on Windows; returns $true when `import curses` works afterwards.
    function Ensure-Curses([string]$py) {
        & $py -c 'import curses' 2>$null
        if ($LASTEXITCODE -eq 0) { return $true }
        Write-Host "    installing windows-curses for $py ..."
        & $py -m pip install --user --disable-pip-version-check -q windows-curses 2>$null | Out-Null
        if ($LASTEXITCODE -ne 0) {   # inside a virtualenv --user is refused
            & $py -m pip install --disable-pip-version-check -q windows-curses 2>$null | Out-Null
        }
        & $py -c 'import curses' 2>$null
        return ($LASTEXITCODE -eq 0)
    }

    Step 'Looking for Python 3.9+'
    $Py = $null
    foreach ($attempt in 1..2) {
        foreach ($cand in (Find-Pythons)) {
            if (Ensure-Curses $cand) { $Py = $cand; break }
            Warn "windows-curses could not be installed for $cand"
        }
        if ($Py -or $attempt -eq 2) { break }

        Warn 'no Python with curses found'
        if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
            Fail 'install Python 3.13 from https://www.python.org/downloads/windows/ and run this installer again'
            return
        }
        $answer = Read-Host '  Install Python 3.13 with winget now? [Y/n]'
        if ($answer -match '^\s*[nN]') {
            Fail 'Python is required. Install it from https://www.python.org/downloads/windows/ and run this installer again.'
            return
        }
        winget install --exact --id Python.Python.3.13 --scope user --accept-package-agreements --accept-source-agreements
        $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
    }
    if (-not $Py) {
        Fail 'no working Python found. Install Python 3.13 (x64) from https://www.python.org/downloads/windows/ and run this installer again.'
        return
    }
    $version = (& $Py -c 'import platform; print(platform.python_version())')
    Ok "$Py ($version)"

    # --- 2. Download the game ---------------------------------------------------------------------
    Step 'Downloading the game'
    $tmp = "$Game.download"
    try {
        New-Item -ItemType Directory -Force -Path $BinDir -ErrorAction Stop | Out-Null
        Invoke-WebRequest -UseBasicParsing -Uri "$RawUrl/flappy.py" -OutFile $tmp -ErrorAction Stop
    } catch {
        Fail "download failed: $RawUrl/flappy.py ($($_.Exception.Message))"
        return
    }
    & $Py -c 'import ast, sys; ast.parse(open(sys.argv[1], encoding=''utf-8'').read())' $tmp 2>$null
    if ($LASTEXITCODE -ne 0) {
        Remove-Item -Force $tmp -ErrorAction SilentlyContinue
        Fail 'the downloaded file is not valid Python (network problem?); nothing was changed'
        return
    }
    try { Move-Item -Force $tmp $Game -ErrorAction Stop } catch { Fail "cannot write $Game ($($_.Exception.Message))"; return }
    Ok $Game

    # --- 3. The `flappy` command ------------------------------------------------------------------
    Step 'Creating the flappy command'
    # cmd.exe reads .cmd files in the OEM code page, so paths under the user folder (which may hold a
    # non-ASCII user name) are written through %LOCALAPPDATA% / %USERPROFILE% instead of literally.
    function To-CmdPath([string]$p) {
        foreach ($var in 'LOCALAPPDATA', 'USERPROFILE') {
            $val = [Environment]::GetEnvironmentVariable($var)
            if ($val -and $p.StartsWith($val, [StringComparison]::OrdinalIgnoreCase)) { return "%$var%" + $p.Substring($val.Length) }
        }
        return $p
    }
    $launcher = "@echo off`r`n" +
                "rem flappy-terminal launcher (made by install.ps1; re-run the installer to update)`r`n" +
                "`"$(To-CmdPath $Py)`" `"$(To-CmdPath $Game)`" %*`r`n"
    try {
        $oem = [System.Text.Encoding]::GetEncoding([Globalization.CultureInfo]::CurrentCulture.TextInfo.OEMCodePage)
    } catch {
        $oem = [System.Text.Encoding]::ASCII
    }
    try { [System.IO.File]::WriteAllText($Command, $launcher, $oem) } catch { Fail "cannot write $Command ($($_.Exception.Message))"; return }
    Ok $Command

    # --- 4. PATH ----------------------------------------------------------------------------------
    try {
        $userParts = Split-PathList (Get-UserPath)
        if ($userParts -notcontains $BinDir) {
            Set-UserPath ((@($userParts) + $BinDir) -join ';')
            Ok "added $BinDir to your user PATH"
        }
    } catch {
        Warn "could not add $BinDir to your user PATH ($($_.Exception.Message)); add it by hand"
    }
    if ((Split-PathList $env:Path) -notcontains $BinDir) { $env:Path = "$env:Path;$BinDir" }

    Write-Host ''
    Write-Host '  Installed!' -ForegroundColor Green
    Write-Host ''
    Write-Host '  Start the game:  ' -NoNewline; Write-Host 'flappy' -ForegroundColor White
    Write-Host '  (works in this window now and in every new terminal; Windows Terminal looks best)'
    Write-Host ''
    Write-Host '  Space / Up = flap   P = pause   Q = quit   flappy --help for options'
    Write-Host '  Update: run the same install command again.'
    Write-Host ''
}
